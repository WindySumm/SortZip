import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from sortzip_core import engine,file_journal,i18n,task_recovery
from sortzip_core.archive_tools import ArchiveTool
from sortzip_core.nested_execution import execute_plan
from sortzip_core.nested_pipeline import list_rows,preflight_tool
from sortzip_core.nesting import Layer,MaterializedUnit,Unit,plan_layers
from sortzip_core.workspace_planning import build_workspace_preview,inspect_resume


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

    def test_managed_classification_recovers_and_cleans_only_task_records(self):
        cancelled = False
        def progress(start,end,done,total,message):
            nonlocal cancelled
            cancelled = start==0 and done==1

        stats = {}
        with patch.object(task_recovery,'datetime') as clock:
            clock.now.return_value = datetime(2026,11,15,14,25,30)
            self.assertEqual(engine.main_from_config(dict(self.config,managed_recovery=True),stats=stats,
                    on_progress=progress,cancel_check=lambda:cancelled),'safe_cancelled')
        legacy = self.checkpoint
        self.checkpoint = engine._checkpoint_path(self.output)
        self.assertEqual(self.checkpoint,self.output/file_journal.RECOVERY/'compression-2026-11-15_142530-doc'/legacy.name)
        self.assertEqual(stats['recovery_path'],str(self.checkpoint))
        self.assertTrue((self.checkpoint.parent/file_journal.DATABASE).is_file())
        self.assertFalse(legacy.exists())
        if os.name=='nt' and file_journal._windows_directory_key(self.output.resolve()):
            self.assertFalse(list(self.output.rglob(file_journal.LOCK)))
        unrelated = self.checkpoint.parent.parent/'notes.txt'
        unrelated.write_bytes(b'keep')
        view = inspect_resume(dict(resume_from=str(self.checkpoint)),0)
        self.assertFalse(view.error)
        self.assertEqual(view.input_root,str(self.source))
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()
        self.assertFalse(self.checkpoint.parent.exists())
        self.assertEqual(unrelated.read_bytes(),b'keep')

    def test_original_fixed_managed_directory_remains_resumable(self):
        cancelled = False
        def progress(start,end,done,total,message):
            nonlocal cancelled
            cancelled = start==0 and done==1
        with patch.object(file_journal,'choose_compression_directory',return_value=file_journal.COMPRESSION):
            self.assertEqual(engine.main_from_config(dict(self.config,managed_recovery=True),
                    on_progress=progress,cancel_check=lambda:cancelled),'safe_cancelled')
        self.checkpoint = engine._checkpoint_path(self.output)
        state = json.loads(self.checkpoint.read_bytes())
        state.pop('recovery_folder')
        engine._write_checkpoint(self.output,state)
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()

    def test_owned_recovery_directory_is_removed_only_after_success(self):
        self.assertEqual(engine.main_from_config(dict(self.config,managed_recovery=True)),'completed')
        self.assertFalse((self.output/file_journal.RECOVERY).exists())
        self.assert_swapped()
        existing = self.output/file_journal.RECOVERY
        existing.mkdir()
        file_journal.remove_empty_recovery(self.output)
        self.assertTrue(existing.is_dir())

    @unittest.skipUnless(os.name=='nt','Windows mutex integration')
    def test_windows_lock_blocks_other_process_and_releases_after_exit(self):
        self.output.mkdir()
        interrupted = False
        def progress(start,end,done,total,message):
            nonlocal interrupted
            interrupted = start==0 and done==1
        self.assertEqual(engine.main_from_config(dict(self.config,managed_recovery=True),
                on_progress=progress,cancel_check=lambda:interrupted),'safe_cancelled')
        self.checkpoint = engine._checkpoint_path(self.output)
        other = self.root/'other'
        other.mkdir()
        alias = self.root/'alias'
        subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(alias),str(self.output)],
                       check=True,capture_output=True)
        self.assertIsNotNone(file_journal._windows_directory_key(self.output.resolve()))
        script = ('import sys; from pathlib import Path; from sortzip_core.file_journal import task_lock\n'
                  'with task_lock(Path(sys.argv[1])):\n'
                  '    print("locked",flush=True); sys.stdin.read(1)\n')
        args = [os.sys.executable,'-u','-c',script]
        with subprocess.Popen([*args,str(self.output)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True) as held:
            try:
                self.assertEqual(held.stdout.readline().strip(),'locked')
                extended = chr(92)*2+'?'+chr(92)+str(self.output)
                for spelling in (str(alias),extended,str(self.output).swapcase()):
                    with self.subTest(spelling=spelling):
                        peer = subprocess.run([*args,spelling],input='',capture_output=True,text=True,timeout=10)
                        self.assertNotEqual(peer.returncode,0)
                        self.assertIn('ValueError',peer.stderr)
                peer = subprocess.run([*args,str(other)],input='',capture_output=True,text=True,timeout=10)
                self.assertEqual(peer.returncode,0,peer.stderr)
                self.assertFalse(list(self.root.rglob(file_journal.LOCK)))
            finally:
                held.kill()
                held.wait(timeout=10)
        with file_journal.task_lock(alias):
            self.assertFalse((self.output/file_journal.LOCK).exists())
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()
        self.assertFalse((self.output/file_journal.RECOVERY).exists())

    @unittest.skipUnless(os.name=='nt','Windows file locking')
    def test_file_lock_fallback_excludes_a_second_process_and_preserves_legacy_lock(self):
        self.assertIsNone(file_journal._windows_directory_key(Path(r'\\server\share\folder')))
        self.output.mkdir()
        legacy = self.output/file_journal.LOCK
        with patch.object(file_journal,'_windows_directory_key',return_value=None),file_journal.task_lock(self.output):
            script = ('import sys; from pathlib import Path; from sortzip_core.file_journal import task_lock\n'
                      'with task_lock(Path(sys.argv[1])): pass')
            peer = subprocess.run([os.sys.executable,'-c',script,str(self.output)],
                                  capture_output=True,text=True,timeout=10)
            self.assertNotEqual(peer.returncode,0)
            self.assertTrue(legacy.is_file())
        with file_journal.task_lock(self.output):
            self.assertTrue(legacy.is_file())

    def test_old_integer_overflow_checkpoint_resumes_with_large_file_id(self):
        identity = file_journal.identity
        big_ids = {identity(self.source/'1.txt')[1]:2**100+1,
                   identity(self.source/'2.txt')[1]:2**63+1}

        def wide_identity(path):
            device,inode,size,mtime = identity(path)
            return device,big_ids.get(inode,inode),size,mtime

        def old_record(db,index,step,file_id=(None,None)):
            with db:
                db.execute('INSERT OR REPLACE INTO operations VALUES (?,?,?,?)',(index,step,*file_id))

        with patch.object(file_journal,'identity',side_effect=wide_identity):
            with patch.object(file_journal,'record',side_effect=old_record),self.assertRaises(ValueError) as error:
                engine.main_from_config(self.config)
            self.assertIn('无法保存文件的恢复记录',str(error.exception))
            self.assertIn(str(self.checkpoint),str(error.exception))
            self.assertNotIn('SQLite INTEGER',str(error.exception))
            self.assertEqual({path.name:path.read_bytes() for path in self.source.iterdir()},
                             {'1.txt':b'one','2.txt':b'two'})
            self.assertTrue(self.checkpoint.is_file())
            self.assertEqual(self.resume(),'completed')
            self.assert_swapped()
            self.assertEqual(self.resume(),'completed')
            state,steps = task_recovery.read_state(self.checkpoint)
            with file_journal.journal(self.output,state['job'],readonly=True) as db:
                recorded = file_journal.ready_identities(db,steps)
                self.assertEqual({inode for _,inode in recorded.values()},set(big_ids.values()))

    def test_journal_handles_legacy_and_large_ids_without_losing_precision(self):
        self.output.mkdir()
        with file_journal.journal(self.output,'sample',create=True) as db:
            values = ((0,2**63-1),(2**127+17,2**63),(None,None))
            for index,value in enumerate(values):
                file_journal.record(db,index,'copy',value)
            states = file_journal._operation_states(db)
            self.assertEqual([states[index][1] for index in range(len(values))],list(values))
            self.assertEqual(db.execute('SELECT typeof(inode) FROM operations ORDER BY id').fetchall(),
                             [('integer',),('text',),('null',)])
            db.execute('UPDATE operations SET inode=? WHERE id=0',(float(2**64),))
            with self.assertRaisesRegex(ValueError,'恢复记录中的文件编号无效'):
                file_journal._operation_states(db)

    def test_recovery_write_failure_uses_selected_language(self):
        raw_message = 'database or disk is full'
        failure = sqlite3.OperationalError(raw_message)
        failure.sqlite_errorcode = sqlite3.SQLITE_FULL
        for language,message in (
            ('zh','磁盘空间不足，无法保存恢复记录。'),
            ('en','There is not enough disk space to save the recovery log.'),
        ):
            with self.subTest(language=language):
                output = self.root/language
                checkpoint = engine._checkpoint_path(output)
                with patch.object(i18n,'_language',language), \
                        patch.object(file_journal,'record',side_effect=failure), \
                        self.assertRaises(ValueError) as error:
                    engine.main_from_config(dict(self.config,dest=str(output)))
                self.assertIn(message,str(error.exception))
                self.assertIn(str(checkpoint),str(error.exception))
                self.assertNotIn(raw_message,str(error.exception))
                self.assertTrue(checkpoint.is_file())
                self.assertEqual({path.name:path.read_bytes() for path in self.source.iterdir()},
                                 {'1.txt':b'one','2.txt':b'two'})

    def test_parent_output_can_reuse_clear_directory_and_resume(self):
        self.output = self.source.parent
        self.checkpoint = engine._checkpoint_path(self.output)
        self.config.update(dest=str(self.output),recursive=True)
        sibling = self.output/'doc'
        sibling.mkdir()
        note = sibling/'unrelated.txt'
        note.write_bytes(b'unrelated')
        view = build_workspace_preview(self.config,0,strict=True)
        self.assertFalse(view.file_error or view.archive_error)
        self.assertEqual(view.total_files,2)
        stopped = False
        def progress(start,end,done,total,message):
            nonlocal stopped
            stopped = start==0 and done==1
        self.assertEqual(engine.main_from_config(self.config,on_progress=progress,cancel_check=lambda:stopped),
                         'safe_cancelled')
        self.assertFalse(inspect_resume(dict(resume_from=str(self.checkpoint)),0).error)
        self.assertEqual(self.resume(),'completed')
        self.assertEqual(self.resume(),'completed')
        self.assert_swapped()
        self.assertEqual(note.read_bytes(),b'unrelated')
        self.assertEqual(list(self.source.iterdir()),[])

    def test_parent_output_stops_conflicts_before_file_operations(self):
        self.config['dest'] = str(self.root)
        sibling = self.root/'doc'
        sibling.mkdir()
        routes = ((self.source.name,self.source,'结果会写进输入文件夹'),
                  ('doc',sibling/'1.txt','输出位置已被占用'),
                  ('doc',sibling/'List.txt','输出位置已被占用'))
        for category,path,message in routes:
            with self.subTest(path=path):
                if path!=self.source:
                    path.write_bytes(b'existing')
                self.config['custom_names']['.txt'] = category
                view = build_workspace_preview(self.config,0)
                self.assertIn(str(path),view.file_error)
                with self.assertRaisesRegex(ValueError,message):
                    engine.main_from_config(self.config)
                if path!=self.source:
                    self.assertEqual(path.read_bytes(),b'existing')
                    path.unlink()
                self.assertFalse(engine._checkpoint_path(self.root).exists())
                self.assertEqual((self.source/'1.txt').read_bytes(),b'one')
                self.assertEqual((self.source/'2.txt').read_bytes(),b'two')

    def test_parent_output_rejects_archive_path_inside_input(self):
        config = dict(self.config,dest=str(self.root),first_compress=True,
                      compression_layers=[Layer()])
        config['custom_names'] = {'.txt':self.source.name}
        view = build_workspace_preview(config,0,strict=True)
        self.assertIn('结果会写进输入文件夹',view.archive_error or view.file_error)
        self.assertIn(str(self.source),view.archive_error or view.file_error)
        with self.assertRaisesRegex(ValueError,'结果会写进输入文件夹'):
            engine.main_from_config(config)
        self.assertEqual((self.source/'1.txt').read_bytes(),b'one')
        self.assertFalse(engine._checkpoint_path(self.root).exists())

    @unittest.skipUnless(os.name=='nt','Windows directory junction')
    def test_parent_output_does_not_write_through_a_junction_to_input(self):
        alias = self.root/'doc'
        subprocess.run(['cmd','/c','mklink','/J',str(alias),str(self.source)],
                       check=True,capture_output=True)
        try:
            self.config['dest'] = str(self.root)
            with self.assertRaisesRegex(ValueError,'链接|结果会写进输入文件夹'):
                engine.main_from_config(self.config)
            self.assertEqual((self.source/'1.txt').read_bytes(),b'one')
            self.assertFalse(engine._checkpoint_path(self.root).exists())
        finally:
            alias.rmdir()

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
        records = list(list_rows(listing))
        self.assertEqual(list(records[0]),['序号','原文件名','新文件名'])
        self.assertEqual({row['原文件名'] for row in records},{'中文 一.txt','中文 二.txt'})
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
