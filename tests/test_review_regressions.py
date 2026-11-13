"""Safety and scaling issues found during the full code review."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.config import prepare_config
from sortzip_core.formats import validate_volume


class ReviewRegressions(unittest.TestCase):
    def test_volume_syntax(self):
        for value in (None, '1', '100k', '10MB', '2gb'):
            validate_volume(value)
        for value in ('', '0', '-1', '1.5m', 'garbage', 12):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_volume(value)

    def test_invalid_active_volume_is_rejected_before_transfer(self):
        output = self.root/'out'
        with self.assertRaises(ValueError):
            engine.main_from_config(dict(src=str(self.folder), dest=str(output),
                first_compress=True, enable_volume=True, volume='invalid', group_size=1))
        self.assertTrue((self.folder/'a.txt').exists())
        self.assertFalse(output.exists())

    def test_inactive_volume_and_suffix_are_ignored(self):
        config = prepare_config(dict(src=str(self.folder), dest=str(self.root/'out'),
            enable_volume=False, volume='invalid', double_compress=False,
            archive_suffix='bad/path', group_size=1))
        self.assertIsNone(config['volume'])
        self.assertNotIn('archive_suffix', config)

    def test_checkpoint_name_is_reserved_case_insensitively(self):
        (self.folder/'.SORTZIP_RESUME.JSON').write_text('{}')
        plan = engine.build_execution_plan(dict(src=str(self.folder), dest=str(self.root/'out'),
            first_compress=False, group_size=1))
        self.assertEqual(len(plan), 2)

    def setUp(self):
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root/'doc'
        self.folder.mkdir()
        (self.folder/'a.txt').write_text('one')
        (self.folder/'b.txt').write_text('two')

    def fake_process(self, cmd, *args):
        if cmd[1] == 'a':
            output = next(Path(arg) for arg in cmd[2:] if not arg.startswith('-'))
            output.write_bytes(b'archive')
        return '', ''

    def compress(self, **kwargs):
        options = dict(dest_root=self.root, group_size=2, password='', enable_volume=False)
        options.update(kwargs)
        return engine.group_compress(**options)

    def stop_after_first(self, keep_files=False):
        calls = [0]
        def cancelled():
            calls[0] += 1
            return calls[0] > 1
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(keep_files=keep_files, cancel_check=cancelled), 'safe_cancelled')
        return engine._checkpoint_path(self.root)

    def test_new_group_task_cannot_overwrite_existing_checkpoint(self):
        engine._write_checkpoint(self.root, {'old': 'untouched'})
        checkpoint = engine._checkpoint_path(self.root)
        before = checkpoint.read_bytes()
        with patch.object(engine, '_run_proc') as process, self.assertRaises(ValueError):
            self.compress()
        process.assert_not_called()
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual((self.folder/'a.txt').read_text(), 'one')

    def test_config_rejects_checkpoint_before_classification(self):
        output = self.root/'out'
        output.mkdir()
        engine._write_checkpoint(output, {'old': True})
        with self.assertRaises(ValueError):
            prepare_config(dict(src=str(self.folder), dest=str(output), group_size=1,
                                first_compress=True, enable_volume=False))
        self.assertEqual((self.folder/'a.txt').read_text(), 'one')
        self.assertFalse((output/'txt').exists())

    def test_recreated_same_size_file_is_not_deleted_on_resume(self):
        checkpoint = self.stop_after_first()
        recreated = self.folder/'a.txt'
        recreated.write_text('NEW')
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(checkpoint=checkpoint), 'completed')
        self.assertEqual(recreated.read_text(), 'NEW')
        self.assertTrue((self.folder/'1-2.zipp').exists())

    def test_keep_files_option_survives_resume(self):
        checkpoint = self.stop_after_first(keep_files=True)
        self.assertTrue(engine._load_checkpoint(checkpoint)['config']['keep_files'])
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(checkpoint=checkpoint), 'completed')
        self.assertEqual((self.folder/'a.txt').read_text(), 'one')
        self.assertEqual((self.folder/'b.txt').read_text(), 'two')

    def test_same_size_change_during_compression_retains_pending_source(self):
        source = self.folder/'a.txt'
        before = source.stat()
        def modified(cmd, *args):
            result = self.fake_process(cmd)
            if cmd[1] == 'a':
                source.write_text('NEW')
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            return result
        with patch.object(engine, '_run_proc', side_effect=modified):
            self.assertEqual(self.compress(), 'error')
        self.assertEqual(source.read_text(), 'NEW')
        data = engine._load_checkpoint(engine._checkpoint_path(self.root))
        self.assertEqual(data['groups'][0]['state'], 'pending')
        self.assertIn('修改时间', engine.validate_resume_files(engine._checkpoint_path(self.root))[0])

    def test_source_is_rechecked_immediately_before_each_transfer(self):
        def progress(start, end, cur, total, message):
            if message.startswith('分类') and cur == 1:
                (self.folder/'b.txt').write_text('changed')
        config = dict(src=str(self.folder), dest=str(self.root/'out'), group_size=1,
                      custom_names={'.txt': 'text'}, first_compress=False, keep_files=True)
        with self.assertRaises(ValueError):
            engine.main_from_config(config, on_progress=progress)
        self.assertFalse((self.root/'out/text/b.txt').exists())
        self.assertEqual((self.folder/'b.txt').read_text(), 'changed')

    def test_artifact_lookup_does_not_rescan_directory_per_group(self):
        for name in ('1.zip', '1.z01', '1.z02'):
            (self.folder/name).write_bytes(b'part')
        with patch.object(Path, 'iterdir', side_effect=AssertionError('unexpected full scan')):
            artifacts = engine._first_artifacts(self.folder, '1', '')
        self.assertEqual([p.name for p in artifacts], ['1.z01', '1.z02', '1.zip'])

    def test_leftover_index_includes_gaps_and_excludes_ordinary_files(self):
        for name in ('1-First.zip', '1-First.z03', '1-First.notes'):
            (self.folder/name).write_bytes(b'part')
        index = engine._index_artifacts([self.folder], '-First', None, 'zip')
        self.assertEqual({p.name for p in index[(self.folder, '1')]}, {'1-First.zip', '1-First.z03'})

    def test_old_checkpoint_without_timestamps_is_still_valid(self):
        data = dict(config={}, groups=[dict(folder='doc', base_name='1-2', state='pending',
                    files=['a.txt', 'b.txt'], sizes={'a.txt': 3, 'b.txt': 3})])
        engine._write_checkpoint(self.root, data)
        self.assertEqual(engine.validate_resume_files(engine._checkpoint_path(self.root)), [])

    def test_invalid_checkpoint_timestamp_is_rejected(self):
        data = dict(config={}, groups=[dict(folder='doc', base_name='1', state='pending',
                    files=['a.txt'], mtimes={'a.txt': 'not-an-integer'})])
        with self.assertRaises(ValueError):
            engine._validate_checkpoint(data, self.root)

    def test_planning_resolves_shared_directories_once(self):
        for index in range(100):
            (self.folder/f'file_{index}.txt').write_text('data')
        original, calls = Path.resolve, []
        def counted(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', counted):
            plan = engine.build_execution_plan(dict(src=str(self.folder), dest=str(self.root/'out'),
                custom_names={'.txt': 'doc'}, first_compress=False, group_size=10))
        self.assertEqual(len(plan), 102)
        self.assertLess(len(calls), 10)

    def test_many_groups_do_not_rescan_folder_for_each_group(self):
        for index in range(30):
            (self.folder/f'file_{index}.txt').write_text('data')
        original, calls = Path.iterdir, []
        def counted(path):
            if path == self.folder:
                calls.append(path)
            return original(path)
        with patch.object(Path, 'iterdir', counted), patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(self.compress(group_size=1, double_compress=False, first_suffix=''), 'completed')
        self.assertLessEqual(len(calls), 3)
