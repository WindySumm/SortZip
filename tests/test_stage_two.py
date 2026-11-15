import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.formats import FORMATS, validate_compression
from sortzip_core.nested_pipeline import list_rows


class StageTwoTests(unittest.TestCase):
    def setUp(self):
        self.log = io.StringIO()
        capture = contextlib.redirect_stdout(self.log)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        scratch = Path(__file__).parent / '__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.src = self.root / 'src'
        self.src.mkdir()
        self.dest = self.root / 'out'
        self.config = dict(src=str(self.src), dest=str(self.dest), custom_names={'.txt': 'doc'},
            group_size=2, password='', archive_format='zip', enable_volume=False, first_compress=True,
            double_compress=False, output_list=True, keep_files=True, sort_by='name', recursive=True,
            naming_rules=[{'match_folder': 'doc', 'template': '{n}{ext}'}])

    def files(self, *names):
        for name in names:
            file = self.src / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(name, encoding='utf-8')

    def fake_process(self, cmd, *args):
        if cmd[1] == 'a':
            output = next(Path(arg) for arg in cmd[2:] if not arg.startswith('-'))
            output.write_bytes(b'archive')
        return '', ''

    def test_plan_manual_order_drives_rename_list_and_compression(self):
        self.files('f1.txt', 'f2.txt', 'f10.txt')
        self.config['preview_order'] = {'doc': ['f10.txt', 'f1.txt', 'f2.txt']}
        plan = engine.build_execution_plan(self.config)
        self.assertEqual([e['source_rel'] for e in plan], ['f10.txt', 'f1.txt', 'f2.txt'])
        self.assertEqual([e['archive'] for e in plan], ['1-2.zip', '1-2.zip', '3.zip'])
        stats, groups = {}, []
        def compressed(cmd, *args):
            if cmd[1] == 'a':
                inputs = [Path(arg) for arg in cmd[2:] if not arg.startswith('-')][1:]
                groups.append([p.read_text(encoding='utf-8') for p in inputs])
            return self.fake_process(cmd)
        with patch.object(engine, '_run_proc', side_effect=compressed):
            self.assertEqual(engine.main_from_config(self.config, stats=stats), 'completed')
        rows = list(list_rows(self.dest / 'doc/List.txt'))
        self.assertEqual([r['原文件名'] for r in rows], ['f10.txt', 'f1.txt', 'f2.txt'])
        self.assertEqual([r['新文件名'] for r in rows], ['1.txt', '2.txt', '3.txt'])
        self.assertEqual([r['所属压缩包名'] for r in rows], [e['archive'] for e in plan])
        self.assertEqual(groups, [['f10.txt', 'f1.txt'], ['f2.txt']])
        self.assertEqual(stats['files_moved'], 3)
        self.assertEqual(stats['files_renamed'], 3)
        self.assertEqual(stats['groups_success'], 2)
        self.assertTrue((self.src / 'f1.txt').exists())

    def test_hierarchy_inherited_rule_and_independent_group_numbers(self):
        self.files('summer/same.txt', 'winter/same.txt', 'winter/other.txt')
        self.config['keep_hierarchy'] = True
        self.config['first_compress'] = False
        self.config['preview_order'] = {'doc/winter': ['winter/same.txt', 'winter/other.txt']}
        plan = engine.build_execution_plan(self.config)
        self.assertEqual([(e['folder'], e['target_name']) for e in plan],
                         [('doc/summer', '1.txt'), ('doc/winter', '1.txt'), ('doc/winter', '2.txt')])
        stats = {}
        self.assertEqual(engine.main_from_config(self.config, stats=stats), 'completed')
        self.assertEqual((self.dest / 'doc/winter/1.txt').read_text(), 'winter/same.txt')
        self.assertEqual((self.dest / 'doc/summer/1.txt').read_text(), 'summer/same.txt')
        self.assertEqual(stats['groups_total'], 0)

    def test_flat_same_names_use_relative_identity(self):
        self.files('a/same.txt', 'b/same.txt')
        self.config['preview_order'] = {'doc': ['b/same.txt', 'a/same.txt']}
        self.config['naming_rules'] = []
        self.config['first_compress'] = False
        plan = engine.build_execution_plan(self.config)
        self.assertEqual([e['source_rel'] for e in plan], ['b/same.txt', 'a/same.txt'])
        self.assertEqual(len({e['classified_name'] for e in plan}), 2)
        self.assertEqual(engine.main_from_config(self.config), 'completed')
        self.assertEqual({p.read_text() for p in (self.dest/'doc').glob('same*.txt')},
                         {'a/same.txt', 'b/same.txt'})

    def test_unsupported_combinations_do_not_move_files(self):
        self.files('a.txt')
        for change in [dict(archive_format='rar'), dict(archive_format='bz2'),
                       dict(archive_format='tar', password='password'),
                       dict(archive_format='lzh', enable_volume=True),
                       dict(archive_format='gz', group_size=2),
                       dict(archive_format='xz', group_size=1, enable_volume=True, double_compress=True)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                engine.main_from_config(dict(self.config, **change))
            self.assertFalse(self.dest.exists())
            self.assertTrue((self.src/'a.txt').exists())

    def test_preview_snapshot_change_is_rejected(self):
        self.files('a.txt')
        self.config['execution_plan'] = engine.build_execution_plan(self.config)
        (self.src/'a.txt').write_text('modified')
        with self.assertRaises(ValueError):
            engine.main_from_config(self.config)
        self.assertFalse(self.dest.exists())

    def test_archive_name_collision_is_rejected_before_move(self):
        self.files('a.txt')
        with self.assertRaises(ValueError):
            engine.main_from_config(dict(self.config, keep_files=False, first_suffix_replace='.txt'))
        self.assertTrue((self.src/'a.txt').exists())
        self.assertFalse(self.dest.exists())

    def test_invalid_suffix_is_rejected_before_move(self):
        self.files('a.txt')
        for suffix in ('../escape', '', '.bad*'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                engine.main_from_config(dict(self.config, double_compress=True, archive_suffix=suffix))
        self.assertFalse(self.dest.exists())

    def test_no_matched_files_has_zero_stats(self):
        stats = {}
        self.assertEqual(engine.main_from_config(self.config, stats=stats), 'completed')
        self.assertEqual(stats['files_moved'], 0)
        self.assertEqual(stats['groups_total'], 0)

    def test_output_parent_does_not_exclude_all_sources(self):
        self.files('a.txt')
        config = dict(self.config, dest=str(self.root), first_compress=False)
        self.assertEqual(len(engine.build_execution_plan(config)), 1)
        self.assertEqual(engine.main_from_config(config), 'completed')
        self.assertEqual((self.root/'doc/1.txt').read_text(), 'a.txt')

    def test_cancel_after_first_archive_counts_no_successful_group(self):
        self.files('a.txt')
        calls, stats = [0], {}
        def cancel():
            calls[0] += 1
            return calls[0] > 1
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            result = engine.group_compress(self.root, 1, '', enable_volume=False,
                double_compress=True, cancel_check=cancel, stats=stats)
        self.assertEqual(result, 'safe_cancelled')
        self.assertEqual((stats['groups_success'], stats['groups_pending']), (0, 1))

    def test_resumed_completed_groups_are_counted_once(self):
        self.files('a.txt', 'b.txt')
        data = dict(config=dict(group_size=1, enable_volume=False, double_compress=False, first_suffix=''), groups=[
            dict(folder='src', base_name='1', files=['a.txt'], state='final_done'),
            dict(folder='src', base_name='2', files=['b.txt'], state='pending')])
        engine._write_checkpoint(self.root, data)
        stats = {}
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(engine.group_compress(self.root, 1, '', checkpoint=engine._checkpoint_path(self.root), stats=stats), 'completed')
        self.assertEqual((stats['groups'], stats['groups_total']), (2, 2))

    def test_partial_stats_and_checkpoint_not_confused_with_success(self):
        self.files('a.txt', 'b.txt', 'c.txt')
        folder = self.src
        groups = [dict(folder='src', base_name=str(i), files=[f'{letter}.txt'],
                       sizes={f'{letter}.txt': len(f'{letter}.txt')}, state='pending')
                  for i, letter in enumerate('abc', 1)]
        data = dict(config=dict(group_size=1, enable_volume=False, double_compress=False, first_suffix=''), groups=groups)
        engine._write_checkpoint(self.root, data)
        (folder/'a.txt').unlink()
        stats = {}
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            status = engine.group_compress(self.root, 1, '', checkpoint=engine._checkpoint_path(self.root), stats=stats)
        self.assertEqual(status, 'partial')
        self.assertEqual((stats['groups_success'], stats['groups_skipped'], stats['groups_failed'], stats['groups_pending']),
                         (2, 1, 0, 0))
        self.assertTrue(engine._checkpoint_path(self.root).exists())

    def test_failed_first_group_stats_leave_remaining_pending(self):
        self.files('a.txt', 'b.txt', 'c.txt')
        stats = {}
        with patch.object(engine, '_run_proc', side_effect=OSError('failure')):
            status = engine.group_compress(self.root, 1, '', enable_volume=False, double_compress=False,
                                           first_suffix='', stats=stats)
        self.assertEqual(status, 'error')
        self.assertEqual((stats['groups_success'], stats['groups_failed'], stats['groups_pending']), (0, 1, 2))

    def test_failed_persist_does_not_count_success(self):
        self.files('a.txt')
        write, count, stats = engine._write_checkpoint, [0], {}
        def fail_second(*args):
            count[0] += 1
            if count[0] == 2:
                raise OSError('disk')
            return write(*args)
        with patch.object(engine, '_write_checkpoint', side_effect=fail_second), \
                patch.object(engine, '_run_proc', side_effect=self.fake_process):
            status = engine.group_compress(self.root, 1, '', enable_volume=False, double_compress=False,
                                           first_suffix='', stats=stats)
        self.assertEqual(status, 'error')
        self.assertEqual(stats['groups_success'], 0)
        self.assertTrue((self.src/'a.txt').exists())

    def test_unchanged_names_do_not_count_as_renamed(self):
        self.files('1.txt', '2.txt')
        stats = {}
        self.assertEqual(engine.main_from_config(dict(self.config, first_compress=False), stats=stats), 'completed')
        self.assertEqual(stats['files_renamed'], 0)

    def test_zip_input_is_not_excluded_from_planned_compression(self):
        (self.src/'input.zip').write_bytes(b'input')
        stats = {}
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            self.assertEqual(engine.main_from_config(dict(self.config, custom_names={'.zip': 'archives'}), stats=stats), 'completed')
        self.assertEqual(stats['groups_success'], 1)

    def test_source_named_list_is_compressed_when_no_generated_list(self):
        self.files('List.txt')
        stats = {}
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            result = engine.main_from_config(dict(self.config, output_list=False, naming_rules=[]), stats=stats)
        self.assertEqual(result, 'completed')
        self.assertEqual(stats['groups_success'], 1)

    def test_suffix_plan_matches_actual_mode(self):
        self.files('a.txt')
        for change, expected in [(dict(archive_format='7z'), '1.7z'),
                (dict(archive_format='7z', first_suffix_replace='.png'), '1.png'),
                (dict(archive_format='7z', double_compress=True, archive_suffix='.masked'), '1.masked')]:
            with self.subTest(change=change):
                self.assertEqual(engine.build_execution_plan(dict(self.config, **change))[0]['archive'], expected)


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION') == '1', 'opt-in Windows integration')
class FormatIntegration(unittest.TestCase):
    setUp = StageTwoTests.setUp
    files = StageTwoTests.files
    def test_real_formats_and_volume_naming(self):
        bz = shutil.which('bz')
        if not bz:
            self.skipTest('bz not installed')
        for fmt, capability in FORMATS.items():
            cases = [(False, False), (False, True)]
            if capability['volume']:
                cases.append((True, capability['multiple']))
            for volume, double in cases:
                with self.subTest(fmt=fmt, volume=volume, double=double):
                    out = self.root / f'{fmt}-{volume}-{double}'
                    folder = out/'doc'
                    folder.mkdir(parents=True)
                    (folder/'a.bin').write_bytes(os.urandom(9000))
                    password = 'test-pass-123' if capability['password'] else ''
                    result = engine.group_compress(out, 1, password, bandizip_path=bz,
                        archive_format=fmt, first_suffix='-First' if double else '',
                        archive_suffix='.masked', first_suffix_replace=None if volume else '.hidden',
                        double_compress=double, enable_volume=volume, volume_size='1k' if volume else None, verify=True)
                    self.assertEqual(result, 'completed', self.log.getvalue())
                    if double:
                        final = folder/'1.masked'
                    elif volume:
                        final = folder/('1.7z.001' if fmt == '7z' else '1.'+fmt)
                    else:
                        final = folder/'1.hidden'
                    self.assertTrue(final.exists())
                    if not volume or double:
                        header = final.read_bytes()
                        if fmt == 'zip': self.assertTrue(header.startswith(b'PK'))
                        elif fmt == '7z': self.assertTrue(header.startswith(b'7z\xbc\xaf\x27\x1c'))
                        elif fmt == 'gz': self.assertTrue(header.startswith(b'\x1f\x8b'))
                        elif fmt == 'xz': self.assertTrue(header.startswith(b'\xfd7zXZ\x00'))
                        elif fmt == 'tar': self.assertEqual(header[257:262], b'ustar')
                        elif fmt == 'lzh': self.assertEqual(header[2:5], b'-lh')

    def test_real_7z_volume_first_done_resume_uses_recorded_format(self):
        bz = shutil.which('bz')
        if not bz:
            self.skipTest('bz not installed')
        self.files('a.txt')
        calls = [0]
        def cancel():
            calls[0] += 1
            return calls[0] > 1
        self.assertEqual(engine.group_compress(self.root, 1, 'test-pass-123', bandizip_path=bz,
            archive_format='7z', enable_volume=True, volume_size='1k', verify=True,
            cancel_check=cancel), 'safe_cancelled')
        path = engine._checkpoint_path(self.root)
        data = engine._load_checkpoint(path)
        self.assertEqual(data['config']['archive_format'], '7z')
        self.assertEqual(data['groups'][0]['state'], 'first_done')
        stats = {}
        self.assertEqual(engine.group_compress(self.root, 1, 'test-pass-123', bandizip_path=bz,
            archive_format='zip', checkpoint=path, stats=stats), 'completed', self.log.getvalue())
        self.assertTrue((self.src/'1.zipp').read_bytes().startswith(b'7z\xbc\xaf\x27\x1c'))
        self.assertEqual(stats['groups_success'], 1)

    def test_real_split_7z_resume(self):
        bz = shutil.which('bz')
        if not bz:
            self.skipTest('bz not installed')
        (self.src/'random.bin').write_bytes(os.urandom(9000))
        calls = [0]
        def cancel():
            calls[0] += 1
            return calls[0] > 1
        self.assertEqual(engine.group_compress(self.root, 1, 'test-pass-123', bandizip_path=bz,
            archive_format='7z', enable_volume=True, volume_size='1k', verify=True,
            cancel_check=cancel), 'safe_cancelled')
        self.assertTrue((self.src/'1-First.7z.001').exists())
        checkpoint = engine._checkpoint_path(self.root)
        before = checkpoint.read_bytes()
        with self.assertRaisesRegex(ValueError,'原归档校验失败'):
            engine.main_from_config(dict(resume_from=str(checkpoint),password='wrong-password',bandizip=bz))
        self.assertEqual(checkpoint.read_bytes(),before)
        self.assertFalse((self.src/'1.zipp').exists())
        self.assertEqual(engine.main_from_config(dict(resume_from=str(checkpoint),password='test-pass-123',bandizip=bz)),
                         'completed')
        self.assertTrue((self.src/'1.zipp').read_bytes().startswith(b'7z\xbc\xaf\x27\x1c'))
