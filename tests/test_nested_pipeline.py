import contextlib
import io
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine,file_journal
from sortzip_core.config import prepare_config
from sortzip_core.nested_pipeline import format_list,list_rows,write_lists


class NestedPipelineTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root/'source'
        (self.source/'sub').mkdir(parents=True)
        for index in range(1,5):
            (self.source/'sub'/f'original_{index}.txt').write_text(f'payload-{index}')
        self.dest = self.root/'out'
        self.config = dict(src=str(self.source), dest=str(self.dest), recursive=True,
            keep_hierarchy=True, keep_files=True, output_list=True,
            custom_names={'.txt':'doc'}, naming_rules=[dict(match_folder='*',template='{n}{ext}')],
            compression_layers=[dict(group_size=1), dict(group_size=2),dict(group_size=2,suffix='.png')])
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__,None,None,None)

    def fake_process(self,cmd,*args):
        if cmd[1]=='a':
            next(Path(p) for p in cmd[2:] if not p.startswith('-')).write_bytes(b'archive')
        return '', ''

    def test_existing_classification_rename_list_and_nested_statistics_share_plan(self):
        stats = {}
        with patch.object(engine,'_run_proc',side_effect=self.fake_process),patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            self.assertEqual(engine.main_from_config(self.config,stats=stats),'completed')
        self.assertEqual((stats['files_moved'],stats['files_renamed'],stats['groups_success']), (4,4,7))
        self.assertEqual(stats['groups_pending'],0)
        self.assertEqual(stats['file_lists'],1)
        rows = list(list_rows(self.dest/'doc/sub/List.txt'))
        self.assertEqual(rows[0]['原文件名'],'sub/original_1.txt')
        self.assertEqual(rows[0]['新文件名'],'1.txt')
        self.assertEqual(rows[0]['所属压缩包名'],'doc/sub/1-2.png')
        self.assertFalse(list(self.dest.glob('layer-*')))
        self.assertTrue((self.dest/'doc/sub/layer-1/1.zip').is_file())
        self.assertTrue((self.dest/rows[0]['所属压缩包名']).is_file())
        self.assertEqual((self.dest/'doc/sub/1.txt').read_text(),'payload-1')

    def test_main_dispatches_cancelled_multilayer_resume(self):
        stopped = []
        def progress(start,end,cur,total,message):
            if message.startswith('多层压缩') and cur==5:
                stopped.append(True)
        with patch.object(engine,'_run_proc',side_effect=self.fake_process),patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            status = engine.main_from_config(self.config,on_progress=progress,cancel_check=lambda:bool(stopped))
        self.assertEqual(status,'safe_cancelled')
        stats = {}
        with patch.object(engine,'_run_proc',side_effect=self.fake_process) as process,patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            status = engine.main_from_config(dict(resume_from=str(engine._checkpoint_path(self.dest))),stats=stats)
        self.assertEqual(status,'completed')
        self.assertEqual(process.call_count,2)
        self.assertEqual((stats['groups_success'],stats['groups_total'],stats['groups_pending']),(7,7,0))
        self.assertEqual(stats['file_lists'],1)

    def test_publication_resumes_after_volume_move_before_checkpoint_update(self):
        output = self.dest/'doc/sub/1-2.png'
        move = file_journal.rename_new
        def interrupt(source,target):
            move(source,target)
            if target==output:
                raise KeyboardInterrupt()
        with patch.object(engine,'_run_proc',side_effect=self.fake_process), \
                patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'), \
                patch.object(file_journal,'rename_new',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
            engine.main_from_config(self.config)
        checkpoint = engine._checkpoint_path(self.dest)
        state = json.loads(checkpoint.read_bytes())
        self.assertTrue(state['publication']['started'])
        self.assertNotIn(state['final_ids'][0],state['publication']['done'])
        self.assertTrue(output.is_file())
        with patch.object(engine,'_run_proc') as process:
            self.assertEqual(engine.main_from_config(dict(resume_from=str(checkpoint))),'completed')
            process.assert_not_called()
        self.assertEqual(output.read_bytes(),b'archive')
        self.assertFalse(list(self.dest.glob('.sortzip-archives-*')))

    def test_publication_keeps_conflicting_volume_and_can_resume(self):
        output = self.dest/'doc/sub/1-2.png'
        def process(command,*args):
            self.fake_process(command,*args)
            if command[1]=='a' and 'layer-3' in ' '.join(command):
                output.write_bytes(b'external')
        stats = {}
        with patch.object(engine,'_run_proc',side_effect=process),patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            self.assertEqual(engine.main_from_config(self.config,stats=stats),'error')
        self.assertEqual(output.read_bytes(),b'external')
        self.assertIn(str(output),stats['error_detail'])
        output.unlink()
        self.assertEqual(engine.main_from_config(dict(resume_from=str(engine._checkpoint_path(self.dest)))),'completed')
        self.assertEqual(output.read_bytes(),b'archive')

    def test_centered_cjk_and_long_paths_wrap_at_column_widths(self):
        header = ('序号','原文件名','新文件名','所属压缩包名','标准入口卷名','归档文件数')
        filenames = ('双宽文件名'*6+'.mp4','a very long filename with spaces.mp4',
                     '视频/课程/very-long-subdirectory/1.7z.001','1.7z.001')
        content = format_list(header,[(1,*filenames,4)]).decode('utf-8')
        lines = content.splitlines()
        self.assertNotIn('\t',content)
        self.assertGreater(len(lines),2)
        for line in lines:
            self.assertEqual(engine._disp_len(line),124)
        self.assertTrue(lines[0].startswith(' 序号   '))
        self.assertEqual(lines[0][-7:],' 归档文件数 ')
        self.assertEqual(lines[1][:6],'  1   ')

    def test_each_cell_in_the_five_twenty_ten_example_is_centered(self):
        content = format_list(('A','B','C'),[('a','a'*10,'aaa')],widths=(5,20,10),separator='  ')
        self.assertEqual(content.decode().splitlines()[1].replace(' ','x'),
                         'xxaxxxx'+'xxxxxaaaaaaaaaaxxxxx'+'xx'+'xxxaaaxxxx')

    def test_one_category_kit_covers_all_flat_archive_groups(self):
        config = dict(self.config,compression_layers=[dict(group_size=1,suffix='.data')],output_list=False)
        with patch.object(engine,'_run_proc',side_effect=self.fake_process), \
                patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            self.assertEqual(engine.main_from_config(config),'completed')
        category = self.dest/'doc'
        launchers = list(category.rglob('*.cmd'))
        self.assertEqual(len(launchers),1)
        self.assertEqual(launchers[0].parent,category)
        self.assertFalse((category/'Restore-Archive-Names').exists())
        lines = launchers[0].read_bytes().splitlines()
        data = json.loads(base64.b64decode(b''.join(lines[lines.index(b'::SORTZIP-MANIFEST')+2:])))
        self.assertEqual([(row['from'],row['to']) for row in data['files']],
                         [(f'sub/{index}.data',f'sub/{index}.zip') for index in range(1,5)])
        self.assertFalse([child for child in (category/'sub').iterdir() if child.is_dir()])
        self.assertFalse((category/'sub/List.txt').exists())

    def test_published_launcher_can_resume_and_older_checkpoint_keeps_folder(self):
        checkpoint = engine._checkpoint_path(self.dest)
        renamed = self.dest/'doc'/'还原压缩包后缀.cmd'
        move = file_journal.rename_new
        def pause_after_launcher(source,target):
            move(source,target)
            if target==renamed:
                raise KeyboardInterrupt()
        with patch.object(engine,'_run_proc',side_effect=self.fake_process), \
                patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'), \
                patch('sortzip_core.i18n._language','zh'), \
                patch.object(file_journal,'rename_new',side_effect=pause_after_launcher), self.assertRaises(KeyboardInterrupt):
            engine.main_from_config(self.config)
        checkpoint = engine._checkpoint_path(self.dest)
        state = json.loads(checkpoint.read_bytes())
        self.assertEqual(state['category_restore_layout'],'category-file')
        before = renamed.read_bytes()
        with patch('sortzip_core.i18n._language','en'):
            self.assertEqual(engine.main_from_config(dict(resume_from=str(checkpoint))),'completed')
        self.assertEqual(renamed.read_bytes(),before)
        self.assertFalse((renamed.parent/'Restore-Names.cmd').exists())
        renamed.unlink()
        state.pop('category_restore_layout')
        engine._write_checkpoint(self.dest,state)
        self.assertEqual(engine.main_from_config(dict(resume_from=str(checkpoint))),'completed')
        self.assertTrue((renamed.parent/'Restore-Archive-Names'/renamed.name).is_file())
        self.assertFalse(renamed.exists())

    def test_root_launcher_conflicts_are_rejected_before_file_operations(self):
        config = dict(self.config,keep_hierarchy=False)
        category = self.dest/'doc'
        category.mkdir(parents=True)
        same_name = category/'Restore-Names.cmd'
        with patch('sortzip_core.i18n._language','en'):
            for directory in (False,True):
                with self.subTest(directory=directory):
                    if directory:
                        same_name.mkdir()
                    else:
                        same_name.write_text('keep')
                    with self.assertRaisesRegex(ValueError,'Restore-Names.cmd'):
                        engine.main_from_config(config)
                    if directory:
                        same_name.rmdir()
                    else:
                        self.assertEqual(same_name.read_text(),'keep')
                        same_name.unlink()
            archive_collision = dict(config,compression_layers=[dict(group_size=4,suffix='.cmd',
                                                                    name_template='Restore-Names')])
            with self.assertRaisesRegex(ValueError,'Restore-Names.cmd'):
                engine.main_from_config(archive_collision)
            source = self.root/'cmd-input'
            source.mkdir()
            original = source/'Restore-Names.cmd'
            original.write_text('existing input')
            classified_collision = dict(config,src=str(source),custom_names={'.cmd':'doc'},naming_rules=[])
            with self.assertRaisesRegex(ValueError,'Restore-Names.cmd'):
                engine.main_from_config(classified_collision)
            self.assertEqual(original.read_text(),'existing input')
        self.assertFalse(engine._checkpoint_path(self.dest).exists())
        self.assertEqual(len(list((self.source/'sub').glob('*.txt'))),4)

    def test_existing_tabbed_list_is_reused_and_missing_list_is_written_aligned(self):
        self.dest.mkdir()
        header = ('序号','原文件名','新文件名')
        rows = [(1,'长中文文件名'*6+'.txt','short.txt')]
        path = self.dest/'List.txt'
        for separator,old in (('  ',True),('\t',True),('\t',False)):
            legacy = format_list(header,rows,separator=separator,legacy=old).replace(b'\n',b'\r\n')
            path.write_bytes(legacy)
            self.assertEqual(write_lists({self.dest:rows},header),1)
            self.assertEqual(path.read_bytes(),legacy)
            self.assertEqual(list(list_rows(path)),[dict(zip(header,('1',*rows[0][1:]),strict=True))])
        path.unlink()
        self.assertEqual(write_lists({self.dest:rows},header),1)
        content = path.read_text(encoding='utf-8')
        self.assertNotIn('\t',content)
        self.assertEqual(list(list_rows(path)),[dict(zip(header,('1',*rows[0][1:]),strict=True))])
        path.write_text('existing unrelated note',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'List.txt'):
            write_lists({self.dest:rows},header)
        self.assertEqual(path.read_text(encoding='utf-8'),'existing unrelated note')

    def test_four_column_compatibility_writer_uses_the_same_layout(self):
        self.dest.mkdir()
        (self.dest/'doc').mkdir()
        rows = [dict(folder='doc',index=1,source_rel='源文件.txt',target_name='新文件.txt',archive='1.zip')]
        engine.write_rename_list(self.dest,[],execution_plan=rows)
        listing = self.dest/'doc/List.txt'
        self.assertEqual(list(list_rows(listing)),[
            {'序号':'1','原文件名':'源文件.txt','新文件名':'新文件.txt','所属压缩包名':'1.zip'}])
        self.assertTrue(listing.read_text(encoding='utf-8').startswith(' 序号   '))

    def test_layer_error_and_existing_output_rejected_before_classification(self):
        invalid = dict(self.config,compression_layers=[dict(suffix='.png'),dict()])
        with self.assertRaises(ValueError):
            engine.main_from_config(invalid)
        self.assertFalse(self.dest.exists())
        occupied = self.dest/'doc/sub/1-2.png'
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b'existing')
        with self.assertRaises(ValueError):
            engine.main_from_config(self.config)
        self.assertEqual(list(occupied.parent.iterdir()),[occupied])
        self.assertTrue((self.source/'sub/original_1.txt').exists())

    def test_preparation_preserves_caller_and_source_identity(self):
        prepared = prepare_config(self.config,require_files=True)
        repeated = prepare_config(prepared,require_files=True)
        self.assertNotIn('_nested_plan',self.config)
        self.assertEqual(prepared['_nested_plan'],repeated['_nested_plan'])
        self.assertEqual(len(prepared['execution_plan']),4)

    def test_compression_disabled_does_not_validate_inactive_layers(self):
        with patch.object(engine,'_run_proc') as process:
            status = engine.main_from_config(dict(self.config,first_compress=False,compression_layers=[]))
        self.assertEqual(status,'completed')
        process.assert_not_called()

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in real Bandizip')
    def test_real_main_pipeline_uses_independent_layer_formats(self):
        if not shutil.which('bz'):
            self.skipTest('bz missing')
        config = dict(self.config,bandizip=shutil.which('bz'),compression_layers=[
            dict(group_size=1,archive_format='7z',verify=True),
            dict(group_size=2,archive_format='zip',verify=True),
            dict(group_size=2,archive_format='7z',suffix='.png',verify=True)])
        stats = {}
        self.assertEqual(engine.main_from_config(config,stats=stats),'completed')
        state = json.loads(engine._checkpoint_path(self.dest).read_text(encoding='utf-8'))
        self.assertEqual([row['settings']['archive_format'] for row in state['tasks']],
                         ['7z']*4+['zip']*2+['7z'])
        self.assertEqual(stats['groups_success'],7)
        final = next(iter(state['final_ids']))
        record = state['completed'][final]
        delivery = self.root/'delivery'
        shutil.copytree(self.dest/'doc',delivery)
        self.assertTrue((delivery/'还原压缩包后缀.cmd').is_file())
        run = subprocess.run(['cmd.exe','/d','/c','还原压缩包后缀.cmd'],cwd=delivery,input=b'\r\n',
                             capture_output=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        entry = next(item['canonical'] for item in record['files'] if item['shown']==record['entry'])
        subprocess.run([config['bandizip'],'t','-y',str(delivery/'sub'/entry)],check=True,capture_output=True,timeout=30)
