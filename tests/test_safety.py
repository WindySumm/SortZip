import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.credentials import load_password, save_password


class SafetyTests(unittest.TestCase):
    def setUp(self):
        captured = contextlib.redirect_stdout(io.StringIO())
        captured.__enter__()
        self.addCleanup(captured.__exit__, None, None, None)
        scratch = Path(__file__).parent / '__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'doc'
        self.folder.mkdir()
        for name, content in [('1.txt', 'one'), ('2.txt', 'two')]:
            (self.folder / name).write_text(content)

    def rename(self, **kwargs):
        return engine.rename_files_in_folders(self.root, naming_rules=[
            {'match_folder': '*', 'template': '{n}{ext}'}], **kwargs)

    def compress(self, **kwargs):
        defaults = dict(dest_root=self.root, group_size=2, password='secret123',
                        enable_volume=False, verify=True)
        defaults.update(kwargs)
        return engine.group_compress(**defaults)

    def fake_process(self, cmd, *args):
        if cmd[1] == 'a':
            output = next(Path(arg) for arg in cmd[2:] if not arg.startswith('-'))
            output.write_bytes(b'archive')
        return '', ''

    def checkpoint(self):
        return json.loads(engine._checkpoint_path(self.root).read_text(encoding='utf-8'))

    def test_swap_preserves_contents_and_order(self):
        order = self.rename(preview_order={'doc': ['2.txt', '1.txt']})
        self.assertEqual((self.folder / '1.txt').read_text(), 'two')
        self.assertEqual((self.folder / '2.txt').read_text(), 'one')
        self.assertEqual([p.name for p in order[self.folder]], ['1.txt', '2.txt'])
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_unchanged_files_are_in_compression_order(self):
        self.assertEqual(len(self.rename()[self.folder]), 2)

    def test_case_only_rename(self):
        (self.folder / '1.txt').rename(self.folder / 'A.txt')
        (self.folder / '2.txt').unlink()
        engine.rename_files_in_folders(self.root, naming_rules=[{'match_folder': '*', 'template': 'a.txt'}])
        self.assertIn('a.txt', [p.name for p in self.folder.iterdir()])
        self.assertEqual((self.folder / 'a.txt').read_text(), 'one')

    def test_duplicate_and_reserved_targets_rejected_before_changes(self):
        for template in ('same.txt', 'List.txt', '../outside.txt', 'CON.txt'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                engine.rename_files_in_folders(self.root, naming_rules=[{'match_folder': '*', 'template': template}])
        self.assertEqual((self.folder / '1.txt').read_text(), 'one')

    def test_external_target_collision(self):
        (self.folder / 'List.txt').write_text('keep')
        with self.assertRaises(ValueError):
            engine.rename_files_in_folders(self.root, naming_rules=[{'match_folder': '*', 'template': 'List.txt'}])
        self.assertEqual((self.folder / 'List.txt').read_text(), 'keep')

    def test_failure_during_commit_rolls_back_cycle(self):
        original, calls = Path.rename, [0]
        def fail_once(source, target):
            calls[0] += 1
            if calls[0] == 4:
                raise OSError('injected')
            return original(source, target)
        with patch.object(Path, 'rename', fail_once), self.assertRaises(OSError):
            self.rename(preview_order={'doc': ['2.txt', '1.txt']})
        self.assertEqual((self.folder / '1.txt').read_text(), 'one')
        self.assertEqual((self.folder / '2.txt').read_text(), 'two')
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_cancel_before_directory_leaves_names(self):
        self.assertIsNone(self.rename(cancel_check=lambda: True))
        self.assertEqual((self.folder / '1.txt').read_text(), 'one')

    def test_checkpoint_replace_failure_preserves_existing(self):
        engine._write_checkpoint(self.root, {'old': True})
        with patch.object(Path, 'replace', side_effect=OSError('disk')), self.assertRaises(OSError):
            engine._write_checkpoint(self.root, {'new': True})
        self.assertEqual(self.checkpoint(), {'old': True})

    def test_checkpoint_flush_failure_preserves_previous_and_sources(self):
        engine._write_checkpoint(self.root, {'old': True})
        with patch.object(engine.os, 'fsync', side_effect=OSError('disk')), self.assertRaises(OSError):
            engine._write_checkpoint(self.root, {'new': True})
        self.assertEqual(self.checkpoint(), {'old': True})
        self.assertTrue((self.folder / '1.txt').exists())

    def test_invalid_json_and_legacy_checkpoint_loading(self):
        path = engine._checkpoint_path(self.root)
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            engine._load_checkpoint(path)
        data = {'config': {}, 'groups': [{'folder': 'doc', 'base_name': '1-2',
                'files': ['1.txt', '2.txt'], 'state': 'pending'}]}
        engine._write_checkpoint(self.root, data)
        self.assertEqual(engine._load_checkpoint(path), data)

    def test_compression_failure_preserves_sources(self):
        with patch.object(engine, '_run_proc', side_effect=FileNotFoundError('missing bz')):
            self.assertEqual(self.compress(), 'error')
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_force_cancel_during_verify_preserves_source(self):
        def cancel(cmd, *args):
            if cmd[1] == 't':
                raise engine.ForceCancelled()
            return self.fake_process(cmd)
        with patch.object(engine, '_run_proc', side_effect=cancel):
            self.assertEqual(self.compress(), 'force_cancelled')
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_missing_first_archive_does_not_complete_resume(self):
        data = {'config': {'verify': False}, 'groups': [{'folder': 'doc', 'base_name': '1-2',
                'files': ['1.txt', '2.txt'], 'state': 'first_done'}]}
        engine._write_checkpoint(self.root, data)
        with patch.object(engine, '_run_proc') as process:
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'error')
        process.assert_not_called()
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'first_done')

    def test_checkpoint_cannot_override_compressor_executable(self):
        data = {'config': {'bandizip': 'untrusted.exe', 'verify': False}, 'groups': [
            {'folder': 'doc', 'base_name': '1-2', 'files': ['1.txt', '2.txt'], 'state': 'pending'}]}
        engine._write_checkpoint(self.root, data)
        with patch.object(engine, '_run_proc', side_effect=self.fake_process) as process:
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'completed')
        self.assertTrue(all(call.args[0][0] == 'bandizip' for call in process.call_args_list))

    def test_checkpoint_initial_failure_does_not_start_compression(self):
        with patch.object(engine, '_write_checkpoint', side_effect=OSError('disk')), \
                patch.object(engine, '_run_proc') as process, self.assertRaises(OSError):
            self.compress()
        process.assert_not_called()
        self.assertTrue((self.folder / '1.txt').exists())

    def test_checkpoint_after_first_failure_preserves_inputs(self):
        write, count = engine._write_checkpoint, [0]
        def fail_second(*args):
            count[0] += 1
            if count[0] == 2:
                raise OSError('disk')
            return write(*args)
        with patch.object(engine, '_write_checkpoint', side_effect=fail_second), \
                patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(), 'error')
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_normal_double_verifies_before_cleanup(self):
        with patch.object(engine, '_run_proc', side_effect=self.fake_process) as process:
            self.assertEqual(self.compress(), 'completed')
        self.assertEqual([p.name for p in self.folder.iterdir()], ['1-2.zipp'])
        self.assertFalse(engine._checkpoint_path(self.root).exists())
        self.assertGreaterEqual(sum(c.args[0][1] == 't' for c in process.call_args_list), 2)

    def test_first_verify_failure_preserves_source_and_pending_checkpoint(self):
        def fail(cmd, *args):
            if cmd[1] == 't':
                raise subprocess.CalledProcessError(1, cmd)
            return self.fake_process(cmd)
        with patch.object(engine, '_run_proc', side_effect=fail):
            self.assertEqual(self.compress(), 'error')
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertTrue((self.folder / '1-2-First.zip').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_second_verify_failure_preserves_first_and_can_resume(self):
        def fail(cmd, *args):
            if cmd[1] == 't' and Path(cmd[-1]).name.startswith('最终压缩'):
                raise subprocess.CalledProcessError(1, cmd)
            return self.fake_process(cmd)
        with patch.object(engine, '_run_proc', side_effect=fail):
            self.assertEqual(self.compress(), 'error')
        self.assertTrue((self.folder / '1-2-First.zip').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'first_done')
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'completed')

    def test_final_checkpoint_failure_keeps_first_archive(self):
        write, count = engine._write_checkpoint, [0]
        def fail_third(*args):
            count[0] += 1
            if count[0] == 3:
                raise OSError('disk')
            return write(*args)
        with patch.object(engine, '_write_checkpoint', side_effect=fail_third), \
                patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(), 'error')
        self.assertTrue((self.folder / '1-2-First.zip').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'first_done')
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'completed')

    def test_custom_suffix_verified_and_unrelated_file_preserved(self):
        (self.folder / '1-2.notes').write_text('unrelated')
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(double_compress=False, first_suffix='',
                            first_suffix_replace='.hidden', only_dirs={self.folder},
                            folder_order={self.folder: [self.folder/'1.txt', self.folder/'2.txt']}), 'completed')
        self.assertTrue((self.folder / '1-2.hidden').exists())
        self.assertEqual((self.folder / '1-2.notes').read_text(), 'unrelated')

    def test_force_cancel_preserves_sources_and_checkpoint(self):
        with patch.object(engine, '_run_proc', side_effect=engine.ForceCancelled):
            self.assertEqual(self.compress(), 'force_cancelled')
        self.assertTrue((self.folder / '1.txt').exists())
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_safe_cancel_and_legacy_resume(self):
        calls = [0]
        def cancelled():
            calls[0] += 1
            return calls[0] > 1
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(cancel_check=cancelled), 'safe_cancelled')
            data = self.checkpoint()
            self.assertEqual(data['groups'][0]['state'], 'first_done')
            data['groups'][0].pop('sizes')  # optional in old checkpoints
            data['config'].pop('verify')
            engine._write_checkpoint(self.root, data)
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'completed')

    def test_invalid_checkpoint_inputs(self):
        valid = {'config': {}, 'groups': [{'folder': 'doc', 'base_name': '1-2',
                 'files': ['1.txt', '2.txt'], 'state': 'pending'}]}
        engine._validate_checkpoint(valid, self.root)
        invalid = [[], {'config': [], 'groups': []}]
        for field, values in [('folder', ['../escape', 'C:/escape', '/absolute', 'doc/../../x']),
                              ('files', [['../escape'], ['C:escape'], ['a/b'], ['CON.txt']]),
                              ('state', ['unknown', None]), ('sizes', [{'1.txt': -1}]),
                              ('base_name', ['*', '../x', '1[abc]'])]:
            for value in values:
                data = copy.deepcopy(valid)
                data['groups'][0][field] = value
                invalid.append(data)
        for config in ({'group_size': 0}, {'group_size': True}, {'verify': 'yes'},
                       {'first_suffix': '/../'}, {'archive_suffix': '.x*'}, {'volume': []}):
            data = copy.deepcopy(valid)
            data['config'] = config
            invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                engine._validate_checkpoint(data, self.root)

    def test_source_output_collision_is_rejected(self):
        with self.assertRaises(ValueError):
            self.compress(double_compress=False, first_suffix='', first_suffix_replace='.txt', group_size=1)
        self.assertEqual((self.folder / '1.txt').read_text(), 'one')

    def test_missing_source_remains_pending(self):
        with patch.object(engine, '_run_proc', side_effect=engine.ForceCancelled):
            self.compress()
        (self.folder / '1.txt').unlink()
        with patch.object(engine, '_run_proc') as process:
            self.assertEqual(self.compress(checkpoint=engine._checkpoint_path(self.root)), 'partial')
        process.assert_not_called()
        self.assertEqual(self.checkpoint()['groups'][0]['state'], 'pending')

    def test_process_errors_and_logs_hide_password(self):
        process = unittest.mock.Mock()
        process.communicate.return_value = ('secret123', 'secret123')
        process.returncode = 1
        with patch.object(engine.subprocess, 'Popen', return_value=process):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                engine._run_proc(['bz', 'a', '-p:secret123', 'out.zip'])
        self.assertNotIn('secret123', str(caught.exception) + caught.exception.stderr + caught.exception.output)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.compress()
        self.assertNotIn('secret123', output.getvalue())


class Settings(dict):
    def value(self, key, default=None, type=None):
        return self.get(key, default)
    def remove(self, key):
        self.pop(key, None)
    def sync(self):
        pass


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.store = unittest.mock.Mock()
        self.store.read.return_value = 'stored'
        self.warn = unittest.mock.Mock()

    def test_migrate_only_after_success(self):
        settings = Settings(remember_pwd=True, password='legacy')
        self.assertEqual(load_password(settings, self.store, self.warn), 'legacy')
        self.store.write.assert_called_once_with('legacy')
        self.assertNotIn('password', settings)

    def test_migration_failure_preserves_legacy(self):
        settings = Settings(remember_pwd=True, password='legacy')
        self.store.write.side_effect = OSError('denied')
        self.assertEqual(load_password(settings, self.store, self.warn), '')
        self.assertEqual(settings['password'], 'legacy')
        self.warn.assert_called_once()

    def test_read_and_read_failure(self):
        settings = Settings(remember_pwd=True)
        self.assertEqual(load_password(settings, self.store, self.warn), 'stored')
        self.store.read.side_effect = OSError('denied')
        self.assertEqual(load_password(settings, self.store, self.warn), '')
        self.warn.assert_called_once()

    def test_disable_deletes_both_stores(self):
        settings = Settings(remember_pwd=False, password='legacy')
        self.assertEqual(load_password(settings, self.store, self.warn), '')
        self.store.delete.assert_called_once()
        self.assertNotIn('password', settings)

    def test_save_failure_is_nonfatal_and_warns(self):
        self.store.write.side_effect = OSError('denied')
        self.assertFalse(save_password(Settings(), self.store, 'new', True, self.warn))
        self.warn.assert_called_once()

    def test_delete_failure_warns_and_removes_plaintext(self):
        self.store.delete.side_effect = OSError('denied')
        settings = Settings(password='legacy')
        self.assertFalse(save_password(settings, self.store, '', False, self.warn))
        self.assertNotIn('password', settings)
        self.warn.assert_called_once()


if __name__ == '__main__':
    unittest.main()
