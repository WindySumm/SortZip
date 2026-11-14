import csv
import errno
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine,file_journal,task_recovery
from sortzip_core.archive_tools import ArchiveTool
from sortzip_core.nested_execution import execute_plan
from sortzip_core.nested_pipeline import preflight_tool
from sortzip_core.nesting import Layer,MaterializedUnit,Unit,plan_layers
from sortzip_core.workspace_planning import inspect_resume


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'input'
        self.source.mkdir()
        for name,data in (('1.txt',b'one'),('2.txt',b'two')):
            (self.source/name).write_bytes(data)
        self.output = self.root/'output'
        self.checkpoint = engine._checkpoint_path(self.output)
        self.config = dict(src=str(self.source),dest=str(self.output),archive_tool='bandizip',
                           custom_names={'.txt':'doc'},first_compress=False,output_list=True,sort_by='name_desc',
                           naming_rules=[dict(match_folder='*',template='{n}{ext}')])

    def resume(self,*,cancel_check=None,**options):
        return engine.main_from_config(dict(resume_from=str(self.checkpoint),
                                            approved_input_root=str(self.source),**options),cancel_check=cancel_check)

    def assert_swapped(self):
        self.assertEqual((self.output/'doc/1.txt').read_bytes(),b'two')
        self.assertEqual((self.output/'doc/2.txt').read_bytes(),b'one')
        self.assertTrue((self.output/'doc/List.txt').is_file())
        self.assertFalse(list((self.output/'doc').glob('.sortzip-*')))

    def test_classification_cancel_reopens_original_plan_and_requires_input_approval(self):
        stopped = False
        def progress(start,end,done,total,message):
            nonlocal stopped
            stopped = start==0 and done==1
        self.assertEqual(engine.main_from_config(self.config,on_progress=progress,cancel_check=lambda:stopped),
                         'safe_cancelled')
        self.assertEqual(len(list(self.source.iterdir())),1)
        view = inspect_resume(dict(resume_from=str(self.checkpoint)),0)
        self.assertFalse(view.error)
        self.assertEqual(view.input_root,str(self.source))
        with self.assertRaisesRegex(ValueError,'确认原输入目录'):
            engine.main_from_config(dict(resume_from=str(self.checkpoint)))
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()
        self.assertEqual(self.resume(),'completed')

    def test_file_list_is_optional_and_classification_columns_remain_compatible(self):
        stats = {}
        self.assertEqual(engine.main_from_config(dict(self.config,output_list=False),stats=stats),'completed')
        self.assertEqual(stats['file_lists'],0)
        self.assertFalse(list(self.output.rglob('List.txt')))
        for name in ('中文 一.txt','中文 二.txt'):
            (self.source/name).write_text(name,encoding='utf-8')
        (self.source/'附注.md').write_text('note',encoding='utf-8')
        self.config['dest'] = str(self.root/'with-list')
        self.config['custom_names']['.md'] = '附注'
        self.assertEqual(engine.main_from_config(self.config,stats=stats),'completed')
        self.assertEqual(stats['file_lists'],2)
        listing = Path(self.config['dest'])/'doc/List.txt'
        with listing.open(encoding='utf-8',newline='') as stream:
            records = list(csv.reader(stream,delimiter='\t'))
        self.assertEqual(records[0],['序号','原文件名','新文件名'])
        self.assertEqual({row[1] for row in records[1:]},{'中文 一.txt','中文 二.txt'})
        self.assertIn('附注.md',(Path(self.config['dest'])/'附注/List.txt').read_text(encoding='utf-8'))

    def test_resume_reconciles_move_and_both_rename_steps_after_uncaught_interruption(self):
        original = file_journal.rename_new
        for number,stage in enumerate(('move','stage','commit')):
            with self.subTest(stage=stage):
                if number:
                    self.output = self.root/stage
                    self.checkpoint = engine._checkpoint_path(self.output)
                    self.config['dest'] = str(self.output)
                    (self.source/'1.txt').write_bytes(b'one')
                    (self.source/'2.txt').write_bytes(b'two')
                def interrupt(source,target):
                    original(source,target)
                    if ((stage=='move' and source.parent==self.source) or
                            (stage=='stage' and target.suffix=='.rename') or
                            (stage=='commit' and source.suffix=='.rename')):
                        raise KeyboardInterrupt()
                with patch.object(file_journal,'rename_new',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
                    engine.main_from_config(self.config)
                self.assertEqual(self.resume(),'completed')
                self.assert_swapped()

    def test_cross_device_copy_write_failure_leaves_original_and_retries(self):
        original = file_journal.rename_new
        def cross_device(source,target):
            if source.parent==self.source:
                raise OSError(errno.EXDEV,'cross-device')
            original(source,target)
        def full(source,target):
            target.write(b'part')
            raise OSError(errno.ENOSPC,'disk full')
        with patch.object(file_journal,'rename_new',side_effect=cross_device), \
                patch.object(shutil,'copyfileobj',side_effect=full),self.assertRaisesRegex(ValueError,'disk full'):
            engine.main_from_config(self.config)
        self.assertEqual((self.source/'2.txt').read_bytes(),b'two')
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()
        self.assertEqual(list(self.source.iterdir()),[])

    def test_initial_checkpoint_failure_and_conflict_do_not_move_or_overwrite(self):
        with patch.object(engine,'_write_checkpoint',side_effect=OSError('checkpoint failed')), \
                self.assertRaisesRegex(ValueError,'checkpoint failed'):
            engine.main_from_config(self.config)
        self.assertEqual(len(list(self.source.iterdir())),2)
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse((self.output/file_journal.DATABASE).exists())
        original = file_journal.rename_new
        def interrupt(source,target):
            original(source,target)
            if source.parent==self.source:
                raise KeyboardInterrupt()
        with patch.object(file_journal,'rename_new',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
            engine.main_from_config(self.config)
        collision = self.output/'doc/1.txt'
        collision.write_bytes(b'external')
        with self.assertRaisesRegex(ValueError,'占用'):
            self.resume()
        self.assertEqual(collision.read_bytes(),b'external')
        self.assertEqual((self.source/'1.txt').read_bytes(),b'one')

    def test_checkpoint_before_journal_can_be_resumed_and_output_lock_is_exclusive(self):
        with patch.object(file_journal,'journal',side_effect=OSError('disk full')),self.assertRaisesRegex(ValueError,'disk full'):
            engine.main_from_config(self.config)
        self.assertTrue(self.checkpoint.exists())
        self.assertFalse((self.output/file_journal.DATABASE).exists())
        with file_journal.task_lock(self.output),self.assertRaisesRegex(ValueError,'正在执行'):
            self.resume()
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()

    def test_copy_and_case_only_rename_keep_original(self):
        (self.source/'1.txt').rename(self.source/'alpha.txt')
        config = dict(self.config,keep_files=True,naming_rules=[dict(match_folder='*',template='ALPHA{ext}')])
        (self.source/'2.txt').unlink()
        self.assertEqual(engine.main_from_config(config),'completed')
        self.assertEqual((self.source/'alpha.txt').read_bytes(),b'one')
        self.assertIn('ALPHA.txt',[path.name for path in (self.output/'doc').iterdir()])
        self.assertEqual(self.resume(),'completed')

    def test_wrong_password_before_first_archive_leaves_checkpoint_and_journal_unchanged(self):
        secret = 'original-password-123'
        config = dict(self.config,first_compress=True,compression_layers=[Layer(password=secret)])
        stopped = False
        def progress(start,end,done,total,message):
            nonlocal stopped
            stopped = start==0 and done==1
        with patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'):
            self.assertEqual(engine.main_from_config(config,on_progress=progress,cancel_check=lambda:stopped),
                             'safe_cancelled')
        database = self.output/file_journal.DATABASE
        old_state = json.loads(self.checkpoint.read_bytes())
        old_state['auto_close'] = False
        self.checkpoint.write_text(json.dumps(old_state),encoding='utf-8')
        before = self.checkpoint.read_bytes(),database.read_bytes()
        self.assertNotIn(secret.encode(),before[0]+before[1])
        self.assertFalse(json.loads(before[0])['completed'])
        with self.assertRaisesRegex(ValueError,'第 1 层密码'):
            self.resume(layer_passwords={0:'different-password-123'})
        self.assertEqual(before,(self.checkpoint.read_bytes(),database.read_bytes()))
        self.assertEqual(self.resume(layer_passwords={0:secret},cancel_check=lambda:True),'safe_cancelled')
        self.assertEqual(before,(self.checkpoint.read_bytes(),database.read_bytes()))
        def compress(command,*args,**kwargs):
            self.assertIn('-y',command)
            next(Path(word) for word in command[2:] if not word.startswith('-')).write_bytes(b'archive')
        with patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'),patch.object(engine,'_run_proc',side_effect=compress):
            self.assertEqual(self.resume(layer_passwords={0:secret}),'completed')
        state = json.loads(self.checkpoint.read_bytes())
        self.assertEqual(len(state['completed']),2)
        self.assertEqual(inspect_resume(dict(resume_from=str(self.checkpoint)),0).encrypted_layers,())
        with patch('sortzip_core.nested_pipeline.check_tool') as tool:
            self.assertEqual(self.resume(),'completed')
        tool.assert_not_called()

    def test_legacy_disguised_volume_password_gate_precedes_new_archives(self):
        target = self.output/'doc'
        target.mkdir(parents=True)
        inputs = {}
        for i in range(2):
            source = target/f'{i}.txt'
            source.write_bytes(b'original')
            inputs[str(i)] = MaterializedUnit((str(source),),True)
        plan = plan_layers([Unit(key,'doc',(key,)) for key in inputs],
                           [Layer(1,'7z',True,'1k',suffix='.png',password='original-password')])
        stopped = False
        def progress(*args):
            nonlocal stopped
            stopped = True
        def create(command,*args,**kwargs):
            name = next(word for word in command[2:] if not word.startswith('-'))
            for suffix in ('.001','.002'):
                Path(name+suffix).write_bytes(b'volume')
        with patch.object(engine,'_run_proc',side_effect=create):
            execute_plan(plan,inputs,self.output,on_progress=progress,cancel_check=lambda:stopped)
        before = self.checkpoint.read_bytes()
        output_files = set(self.output.rglob('*.png.*'))
        def wrong(command,*args,**kwargs):
            self.assertEqual(command[1],'t')
            self.assertTrue(command[-1].endswith('.7z.001'))
            first = Path(command[-1])
            self.assertEqual(first.read_bytes(),b'volume')
            self.assertTrue(first.with_suffix('.002').is_file())
            raise subprocess.CalledProcessError(2,command)
        with patch('sortzip_core.nested_pipeline.check_tool',return_value='bz'),patch.object(engine,'_run_proc',side_effect=wrong):
            with self.assertRaisesRegex(ValueError,'原归档校验失败'):
                self.resume(layer_passwords={0:'incorrect-password'})
        self.assertEqual(self.checkpoint.read_bytes(),before)
        self.assertEqual(set(self.output.rglob('*.png.*')),output_files)

    def test_attested_tool_is_not_probed_twice_and_replacement_stops_execution(self):
        executable = self.root/'bz.exe'
        executable.write_bytes(b'old tool')
        config = dict(_check_archive_executable=True,archive_tool_paths={'bandizip':str(executable)})
        with patch('sortzip_core.nested_pipeline.check_tool',return_value=str(executable)) as check:
            preflight_tool(config)
            preflight_tool(config)
        check.assert_called_once()
        executable.write_bytes(b'new tool')
        with self.assertRaisesRegex(ValueError,'重新预检'):
            preflight_tool(config)
