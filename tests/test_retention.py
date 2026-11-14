import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sortzip_core import disk_space, engine, retention
from sortzip_core.archive_tools import detect_tool
from sortzip_core.nesting import Layer
from sortzip_core.workspace_planning import inspect_resume


class RetentionTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'input'
        self.source.mkdir()
        self.output = self.root/'output'
        self.checkpoint = engine._checkpoint_path(self.output)
        self.payload = {'中文 = Alpha.txt':os.urandom(5120),'Beta.txt':os.urandom(5120)}
        for name,data in self.payload.items():
            (self.source/name).write_bytes(data)
        self.config = dict(src=str(self.source),dest=str(self.output),archive_tool='bandizip',
            first_compress=True,output_list=True,custom_names={'.txt':'docs'},retain_layers=[],keep_files=True,
            naming_rules=[dict(enable=True,match_folder='*',template='renamed_{n}{ext}')],
            compression_layers=(Layer(1,'zip'),Layer(2,'zip'),Layer(1,'zip')))

    def state(self):
        return json.loads(self.checkpoint.read_text(encoding='utf-8'))

    def resume(self,**options):
        return engine.main_from_config(dict(resume_from=str(self.checkpoint),
            archive_tool_paths=self.config.get('archive_tool_paths')),**options)

    def fake_zip(self,command,force_cancel_check=None,**options):
        if command[1] in ('t','l'):
            with zipfile.ZipFile(command[-1]) as archive:
                if command[1]=='l':
                    return '\n'.join(archive.namelist()),''
                self.assertIsNone(archive.testzip())
            return
        archive = next(index for index,arg in enumerate(command[2:],2) if not arg.startswith('-'))
        with zipfile.ZipFile(command[archive],'w') as output:
            for name in command[archive+1:]:
                output.write(name,Path(name).name)

    def test_cleanup_replays_after_unlink_and_reuses_completed_groups(self):
        unlink = Path.unlink
        def interrupt(path,*args,**kwargs):
            unlink(path,*args,**kwargs)
            if (self.output/'layer-1') in path.parents:
                raise KeyboardInterrupt()
        with patch.object(engine,'_run_proc',side_effect=self.fake_zip):
            with patch.object(Path,'unlink',interrupt),self.assertRaises(KeyboardInterrupt):
                engine.main_from_config(self.config)
            state = self.state()
            self.assertEqual(set(state['cleanup'].values()),{'pending','done'})
            self.assertEqual(len(state['completed']),3)
            self.assertFalse(inspect_resume(dict(resume_from=str(self.checkpoint)),0).error)
            with patch('sortzip_core.task_recovery.nested_pipeline.preflight_tool'):
                self.assertEqual(self.resume(),'completed')
                self.assertEqual(self.resume(),'completed')
        self.assertEqual(set(self.state()['cleanup'].values()),{'done'})
        self.assertFalse(list((self.output/'layer-1').rglob('*.zip')))
        self.assertFalse(list((self.output/'layer-2').rglob('*.zip')))
        self.assertTrue((self.output/'docs/List.txt').is_file())
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()},self.payload)

    def test_list_republishes_without_working_files_and_keeps_conflicting_file(self):
        listing = self.output/'docs/List.txt'
        with patch.object(engine,'_run_proc',side_effect=self.fake_zip):
            self.assertEqual(engine.main_from_config(self.config),'completed')
        original = listing.read_bytes()
        self.assertFalse(list((self.output/'docs').glob('renamed_*.txt')))
        listing.unlink()
        stats = {}
        with patch.object(engine,'_run_proc') as process:
            self.assertEqual(self.resume(stats=stats),'completed')
            self.assertEqual(stats['file_lists'],1)
            self.assertEqual(listing.read_bytes(),original)
            timestamp = listing.stat().st_mtime_ns
            self.assertEqual(self.resume(stats=stats),'completed')
            self.assertEqual(stats['file_lists'],1)
            self.assertEqual(listing.stat().st_mtime_ns,timestamp)
            listing.write_bytes(b'existing list')
            self.assertEqual(self.resume(stats=stats),'error')
            self.assertIn(str(listing),stats['error_detail'])
            self.assertNotIn('file_lists',stats)
            self.assertEqual(listing.read_bytes(),b'existing list')
            process.assert_not_called()

    def test_no_deletion_after_archive_verification_or_checkpoint_failure(self):
        self.config.update(keep_files=False)
        def fail_test(command,*args,**options):
            if command[1]=='t':
                raise subprocess.CalledProcessError(2,command)
            return self.fake_zip(command,*args,**options)
        with patch.object(engine,'_run_proc',side_effect=fail_test):
            self.assertEqual(engine.main_from_config(self.config),'error')
        self.assertEqual(self.state()['cleanup'],{})
        self.assertEqual(len(list((self.output/'docs').glob('*.txt'))),2)
        save = engine._write_checkpoint
        def fail_intent(root,state):
            if 'pending' in state.get('cleanup',{}).values():
                raise OSError('injected disk full')
            save(root,state)
        with patch.object(engine,'_run_proc',side_effect=self.fake_zip),patch.object(engine,'_write_checkpoint',side_effect=fail_intent),patch(
                'sortzip_core.task_recovery.nested_pipeline.preflight_tool'):
            self.assertEqual(self.resume(),'error')
        self.assertEqual(self.state()['cleanup'],{})
        self.assertEqual(len(list((self.output/'docs').glob('*.txt'))),2)

    def test_disk_copy_shortfall_blocks_and_compression_shortfall_warns(self):
        self.config['compression_layers'] = (Layer(2,'zip'),)
        config = engine.prepare_config(self.config)
        with patch.object(disk_space,'disk_usage',return_value=SimpleNamespace(free=8000)):
            with self.assertRaises(ValueError):
                disk_space.inspect(config)
            report, warning = disk_space.inspect(dict(config,keep_files=False))
            self.assertTrue(warning)
            self.assertIn('7.81 KiB',report)
            self.assertNotIn(str(self.source/'Alpha.txt'),report)

    def test_member_failures_leave_inputs_and_checkpoint_unchanged(self):
        self.config.update(compression_layers=(Layer(2,'zip'),),keep_files=False)
        for index,listing in enumerate(('renamed_1.txt', 'renamed_1.txt\nrenamed_2.txt\nextra.txt',
                'renamed_1.txt\nrenamed_2.txt\nrenamed_1.txt', 'other/renamed_1.txt\nrenamed_2.txt',None)):
            with self.subTest(listing=listing):
                for name,data in self.payload.items():
                    (self.source/name).write_bytes(data)
                self.output = self.root/f'bad-{index}'
                self.checkpoint = engine._checkpoint_path(self.output)
                self.config['dest'] = str(self.output)
                def compressor(command,*args,**kwargs):
                    if command[1]=='l':
                        if listing is None:
                            raise subprocess.CalledProcessError(1,command)
                        return listing,''
                    return self.fake_zip(command,*args,**kwargs)
                with patch.object(engine,'_run_proc',side_effect=compressor):
                    self.assertEqual(engine.main_from_config(self.config),'error')
                self.assertEqual(self.state()['completed'],{})
                self.assertEqual(self.state()['cleanup'],{})
                self.assertEqual(len(list((self.output/'docs').glob('*.txt'))),2)

    def test_old_checkpoint_skips_pending_cleanup_but_new_groups_clean(self):
        save = engine._write_checkpoint
        def interrupt(root,state):
            if state.get('cleanup') and not any(step=='done' for step in state['cleanup'].values()):
                first = next(iter(state['completed'].values()))
                first.pop('members_verified')
                save(root,state)
                raise KeyboardInterrupt()
            save(root,state)
        with patch.object(engine,'_run_proc',side_effect=self.fake_zip):
            with patch.object(engine,'_write_checkpoint',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
                engine.main_from_config(self.config)
            view = inspect_resume(dict(resume_from=str(self.checkpoint)),0)
            self.assertEqual(view.preserved_units,1)
            with patch('sortzip_core.task_recovery.nested_pipeline.preflight_tool'):
                self.assertEqual(self.resume(),'completed')
        self.assertEqual(len(list((self.output/'docs').glob('renamed_*.txt'))),1)
        self.assertEqual(len(self.state()['completed']),4)

    def test_successful_compressor_with_missing_volume_preserves_group_inputs(self):
        self.config.update(compression_layers=(Layer(2,'zip',True,'4k'),),keep_files=False)
        def compressor(command,*args,**kwargs):
            self.fake_zip(command,*args,**kwargs)
            archive = next(Path(arg) for arg in command[2:] if not arg.startswith('-'))
            archive.with_suffix('.z02').write_bytes(b'volume two, without volume one')
        with patch.object(engine,'_run_proc',side_effect=compressor):
            self.assertEqual(engine.main_from_config(self.config),'error')
        self.assertEqual(self.state()['completed'],{})
        self.assertEqual(self.state()['cleanup'],{})
        self.assertEqual(len(list((self.output/'docs').glob('renamed_*.txt'))),2)

    def test_peak_reports_stage_and_kept_layers(self):
        config = engine.prepare_config(self.config)
        base,_ = disk_space.inspect(config)
        mixed,_ = disk_space.inspect(dict(config,retain_layers=[0,1]))
        self.assertIn('第 3 层压缩',mixed)
        self.assertIn('预计占用最大的保留内层',mixed)
        self.assertNotIn('预计占用最大的保留内层',base)

    def test_single_stream_hash_detects_different_payload(self):
        import gzip
        import lzma
        from sortzip_core.archive_members import verify
        from sortzip_core.archive_tools import ArchiveTool

        source = next(self.source.iterdir())
        tool = ArchiveTool('bandizip','bz')
        for fmt,compress in (('gz',gzip.compress),('xz',lzma.compress)):
            with self.subTest(fmt=fmt):
                archive = self.root/f'different-name.{fmt}'
                original = source.read_bytes()
                archive.write_bytes(compress(original))
                verify(tool,archive,Layer(1,fmt),[source],None)
                archive.write_bytes(compress(b'!'*len(original)))
                with self.assertRaisesRegex(ValueError,'单流归档内容校验失败'):
                    verify(tool,archive,Layer(1,fmt),[source],None)

    def test_cross_volume_copy_shortfall_counts_the_destination(self):
        config = engine.prepare_config(dict(self.config,keep_files=False))
        source_device = next(self.source.iterdir()).stat().st_dev
        with patch.object(disk_space,'volume',return_value=(source_device+1,self.output)),patch.object(
                disk_space,'disk_usage',return_value=SimpleNamespace(free=8000)):
            with self.assertRaises(ValueError):
                disk_space.inspect(config)

    def test_runtime_disk_full_can_resume_copy(self):
        self.config['first_compress'] = False
        with patch.object(disk_space,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError,'断点'):
                engine.main_from_config(self.config)
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()},self.payload)
        self.assertFalse(list((self.output/'docs').glob('*.copy')))
        self.assertEqual(engine.main_from_config(dict(resume_from=str(self.checkpoint),
            approved_input_root=str(self.source))),'completed')
        self.assertEqual(len(list((self.output/'docs').glob('renamed_*.txt'))),2)

    def test_embedded_script_rejects_reserved_member_and_single_stream_format(self):
        config = dict(self.config,restore_original_names=True,compression_layers=(Layer(1,'gz'),))
        with self.assertRaisesRegex(ValueError,'GZ'):
            engine.prepare_config(config)
        config.update(compression_layers=(Layer(1,'zip'),),naming_rules=[
            dict(enable=True,match_folder='*',template='SortZip-Restore-Names.json')])
        with self.assertRaisesRegex(ValueError,'SortZip-Restore-Names.json'):
            engine.prepare_config(config)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in Qt integration tests')
    def test_final_output_shortcuts_keep_layer_selection_across_structure_edits(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication, QPushButton
        from sortzip_core.smoke import _MemoryCredentials
        from sortzip_core.workbench import WorkbenchWindow

        app = QApplication.instance() or QApplication([])
        settings = QSettings(str(self.root/'ui.ini'),QSettings.Format.IniFormat)
        window = WorkbenchWindow(settings=settings,credential_store=_MemoryCredentials(),check_updates=False)
        try:
            window.add_layer()
            window.add_layer()
            window.navigate('final')
            window.retention_shortcuts.itemAt(1).widget().click()
            self.assertTrue(window.state.to_config()['keep_files'])
            self.assertEqual(window.state.to_config()['retain_layers'],[0,1])
            window.retention_shortcuts.itemAt(0).widget().click()
            self.assertFalse(window.state.to_config()['keep_files'])
            self.assertEqual(window.state.to_config()['retain_layers'],[])
            window.retention_layout.itemAt(1).widget().findChild(QPushButton).click()
            window.restore_original_names.click()
            self.assertEqual(window.state.to_config()['retain_layers'],[0,1])
            self.assertTrue(window.state.to_config()['restore_original_names'])
            window.navigate(window.state.layers[0].key)
            window.remove_layer()
            self.assertEqual(window.state.to_config()['retain_layers'],[0])
            window.state.save(settings)
            from sortzip_core.workspace_state import load_workspace
            reloaded = load_workspace(settings).to_config()
            self.assertTrue(reloaded['restore_original_names'])
            self.assertEqual(reloaded['retain_layers'],[0])
        finally:
            window.close()
            app.processEvents()

    def test_safe_cancel_keeps_unconsumed_files_and_replacement_stops_resume(self):
        def progress(start,end,done,total,message):
            nonlocal stopped
            stopped = start==40 and done==1
        stopped = False
        with patch.object(engine,'_run_proc',side_effect=self.fake_zip):
            self.assertEqual(engine.main_from_config(self.config,on_progress=progress,cancel_check=lambda:stopped),
                             'safe_cancelled')
        state = self.state()
        self.assertEqual(len(state['cleanup']),1)
        sources = [next(iter(paths)) for key,paths in state['sources'].items() if key not in state['cleanup']]
        self.assertEqual(len(sources),1)
        self.assertTrue(Path(sources[0]).is_file())
        archive = next(iter(state['completed'].values()))
        original = self.output/archive['folder']/archive['entry']
        original.rename(original.with_suffix('.old'))
        original.write_bytes(b'replacement')
        with self.assertRaises(ValueError):
            self.resume()
        self.assertEqual(original.read_bytes(),b'replacement')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in real command-line tools')
    def test_real_encrypted_split_retention_and_embedded_restore(self):
        tools = {'bandizip':os.environ.get('SORTZIP_TEST_BANDIZIP'),
                 '7zip':os.environ.get('SORTZIP_TEST_7ZIP'),
                 'winrar':os.environ.get('SORTZIP_TEST_WINRAR')}
        exercised = []
        secret = 'test space &42'
        for tool,hint in tools.items():
            exe = detect_tool(tool,hint or '')
            if not exe:
                continue
            with self.subTest(tool=tool):
                exercised.append(tool)
                output = self.root/tool
                self.checkpoint = engine._checkpoint_path(output)
                fmt = {'bandizip':'7z','7zip':'7z','winrar':'rar'}[tool]
                layers = (Layer(2,fmt,True,'4k',password=secret),Layer(1,fmt),Layer(1,fmt,True,'6k',suffix='.data'))
                config = dict(self.config,dest=str(output),archive_tool=tool,
                    archive_tool_paths={tool:exe},compression_layers=layers,retain_layers=[1],
                    restore_original_names=True)
                stopped = False
                def progress(start,end,done,total,message):
                    nonlocal stopped
                    stopped = start==40 and done==2
                self.assertEqual(engine.main_from_config(config,on_progress=progress,cancel_check=lambda:stopped),
                                 'safe_cancelled')
                self.assertFalse(list((output/'layer-1').rglob('*.001')))
                resume = json.dumps(dict(resume_from=str(self.checkpoint),archive_tool_paths={tool:exe}))
                child = subprocess.run([sys.executable,'-c',
                    'import json,sys; from sortzip_core.engine import main_from_config; '
                    'sys.exit(main_from_config(json.loads(sys.argv[1])) != "completed")',resume],
                    capture_output=True,timeout=30)
                self.assertEqual(child.returncode,0,child.stdout+child.stderr)
                state = self.state()
                self.assertEqual(state['schema'],retention.SCHEMA)
                records = list(state['completed'].values())
                self.assertFalse(any((output/records[0]['folder']/row['shown']).exists() for row in records[0]['files']))
                self.assertGreater(len(records[0]['files']),1)
                self.assertTrue((output/records[1]['folder']/records[1]['entry']).is_file())
                outer = records[2]
                self.assertGreater(len(outer['files']),1)
                with (output/'docs/List.txt').open(encoding='utf-8',newline='') as stream:
                    reader = csv.DictReader(stream,delimiter='\t')
                    rows = list(reader)
                self.assertEqual(reader.fieldnames,['序号','原文件名','新文件名','所属压缩包名','标准入口卷名','归档文件数'])
                self.assertEqual({row['原文件名'] for row in rows},set(self.payload))
                canonical_entry = next(row['canonical'] for row in outer['files'] if row['shown']==outer['entry'])
                volumes = self.root/f'{tool}-standard'
                volumes.mkdir()
                for row in outer['files']:
                    (volumes/row['canonical']).write_bytes((output/outer['folder']/row['shown']).read_bytes())
                for row in rows:
                    self.assertEqual(row['所属压缩包名'],outer['folder']+'/'+outer['entry'])
                    self.assertTrue((output/row['所属压缩包名']).is_file())
                    self.assertEqual(row['标准入口卷名'],canonical_entry)
                    self.assertEqual(int(row['归档文件数']),len(outer['files']))
                entry = volumes/canonical_entry
                for stage,record in enumerate(reversed(records)):
                    unpack = self.root/f'{tool}-{stage}'
                    unpack.mkdir()
                    args = {'bandizip':['-o:'+str(unpack),str(entry)],
                            '7zip':[str(entry),'-o'+str(unpack)],
                            'winrar':['-cfg-',str(entry),str(unpack)+os.sep]}[tool]
                    if stage==2:
                        args.insert(0,('-p:' if tool=='bandizip' else '-p')+secret)
                    result = subprocess.run([exe,'x','-y',*args],stdin=subprocess.DEVNULL,
                                            capture_output=True,timeout=20)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    if stage<2:
                        entry = unpack/records[1-stage]['entry']
                script = unpack/'SortZip-Restore-Names.ps1'
                for _ in range(2):
                    result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                                             '-File',str(script)],capture_output=True,timeout=20)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual({name:(unpack/name).read_bytes() for name in self.payload},self.payload)
        if not exercised:
            self.skipTest('No configured archive tool')
