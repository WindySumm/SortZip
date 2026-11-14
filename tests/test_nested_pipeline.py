import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.config import prepare_config


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
        with (self.dest/'doc/sub/List.txt').open(encoding='utf-8',newline='') as stream:
            rows = list(csv.DictReader(stream,delimiter='\t'))
        self.assertEqual(rows[0]['原文件名'],'sub/original_1.txt')
        self.assertEqual(rows[0]['新文件名'],'1.txt')
        self.assertEqual(rows[0]['所属压缩包名'],'layer-3/doc/sub/1-2/1-2.png')
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

    def test_layer_error_and_existing_output_rejected_before_classification(self):
        invalid = dict(self.config,compression_layers=[dict(suffix='.png'),dict()])
        with self.assertRaises(ValueError):
            engine.main_from_config(invalid)
        self.assertFalse(self.dest.exists())
        occupied = self.dest/'layer-1/doc/sub/1'
        occupied.mkdir(parents=True)
        with self.assertRaises(ValueError):
            engine.main_from_config(self.config)
        self.assertFalse((self.dest/'doc').exists())
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
        shutil.copytree(self.dest/record['folder'],delivery)
        kit = delivery/'Restore-Archive-Names'
        self.assertTrue((kit/'Restore-Names.cmd').is_file())
        run = subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass',
            '-File',str(kit/'Restore-Names.ps1')],capture_output=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        entry = next(item['canonical'] for item in record['files'] if item['shown']==record['entry'])
        subprocess.run([config['bandizip'],'t','-y',str(delivery/entry)],check=True,capture_output=True,timeout=30)
