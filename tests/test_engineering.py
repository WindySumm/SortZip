"""Portable regressions migrated from the ignored local scripts; no Qt required."""
import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sortzip_core import config, engine, planning
from sortzip_core.constants import RESUME_FILE_NAME


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'src'
        self.source.mkdir()
        (self.source/'a.txt').write_text('a', encoding='utf-8')
        self.config = dict(src=str(self.source), dest=str(self.root/'out'),
                           custom_names={'.txt': 'doc'}, first_compress=False, group_size=1)

    def test_engine_preserves_legacy_planning_exports(self):
        for name in ('build_execution_plan', 'render_template', '_sort_files',
                     '_match_rule', '_match_rule_hierarchy', 'check_naming_conflicts'):
            self.assertIs(getattr(engine, name), getattr(planning, name))

    def test_preparation_does_not_mutate_caller_and_reuses_confirmed_plan(self):
        prepared = config.prepare_config(self.config)
        self.assertNotIn('execution_plan', self.config)
        self.assertEqual(len(prepared['execution_plan']), 1)
        self.assertIs(config.prepare_config(prepared)['execution_plan'], prepared['execution_plan'])

    def test_config_errors_have_ui_titles(self):
        for changes in (dict(src=''), dict(dest='')):
            with self.assertRaises(config.ConfigurationError) as caught:
                config.prepare_config(dict(self.config, **changes))
            self.assertTrue(caught.exception.title)

    def test_interactive_duplicate_names_require_hierarchy_before_any_transfer(self):
        for relative in ('a/Same.txt', 'b/same.txt'):
            path = self.source/relative
            path.parent.mkdir()
            path.write_text(relative)
        task = dict(self.config, recursive=True, keep_hierarchy=False)
        for prepared in (task, config.prepare_config(task)):
            with self.assertRaises(config.ConfigurationError) as caught:
                config.prepare_config(prepared, require_files=True)
            self.assertEqual(caught.exception.title, '重名文件检测')
        self.assertFalse((self.root/'out').exists())
        self.assertTrue((self.source/'a/Same.txt').exists())
        result = config.prepare_config(dict(task, keep_hierarchy=True), require_files=True)
        self.assertEqual(len(result['execution_plan']), 3)
        # Noninteractive API keeps its unique-name compatibility policy.
        flat = config.prepare_config(task)['execution_plan']
        self.assertEqual(len({e['classified_name'].casefold() for e in flat}), 3)

    def test_duplicate_policy_only_checks_selected_categories(self):
        for relative in ('a/ignored.bin', 'b/ignored.bin'):
            path = self.source/relative
            path.parent.mkdir()
            path.write_text(relative)
        result = config.prepare_config(dict(self.config, recursive=True), require_files=True)
        self.assertEqual(len(result['execution_plan']), 1)

    def test_password_rules_are_shared(self):
        for password, confirmation, valid in [('', '', True), ('short', 'short', False),
                ('12345678', 'different', False), ('12345678', '12345678', True)]:
            self.assertEqual(config.validate_password(password, confirmation) is None, valid)
        with self.assertRaises(config.ConfigurationError):
            config.prepare_config(dict(self.config, first_compress=True, password='12345678'), confirmation='different')

    def test_interactive_policy_rejects_empty_or_existing_categories(self):
        with self.assertRaises(config.ConfigurationError):
            config.prepare_config(dict(self.config, custom_names={'.png': 'images'}), require_files=True)
        (self.root/'out/doc').mkdir(parents=True)
        with self.assertRaises(config.ConfigurationError):
            config.prepare_config(self.config, require_files=True)
        self.assertEqual(len(config.prepare_config(self.config)['execution_plan']), 1)

    def test_resume_file_excluded_even_when_json_mapping_is_enabled(self):
        checkpoint = self.source/RESUME_FILE_NAME
        checkpoint.write_text('{}', encoding='utf-8')
        cfg = dict(self.config, custom_names={'.txt': 'doc', '.json': 'json'})
        plan = planning.build_execution_plan(cfg)
        self.assertEqual([entry['source_rel'] for entry in plan], ['a.txt'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main_from_config(cfg), 'completed')
        self.assertTrue(checkpoint.exists())

    def test_force_cancel_terminates_real_child_process(self):
        started = time.monotonic()
        with self.assertRaises(engine.ForceCancelled):
            engine._run_proc([sys.executable, '-c', 'import time; time.sleep(30)'],
                             force_cancel_check=lambda: True, wait_ms=50)
        self.assertLess(time.monotonic() - started, 5)

    def test_all_completed_checkpoint_is_a_noop_and_cleared(self):
        output = self.root/'out'
        (output/'doc').mkdir(parents=True)
        engine._write_checkpoint(output, dict(config={}, groups=[
            dict(folder='doc', base_name='1', files=['a.txt'], state='final_done')]))
        stats = {}
        with patch.object(engine, '_run_proc') as process, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main_from_config(dict(resume_from=str(engine._checkpoint_path(output))), stats=stats), 'completed')
        process.assert_not_called()
        self.assertFalse(engine._checkpoint_path(output).exists())
        self.assertEqual(stats['groups_success'], 1)

    def test_modified_resume_source_remains_pending(self):
        engine._write_checkpoint(self.root, dict(config={}, groups=[
            dict(folder='src', base_name='1', files=['a.txt'], sizes={'a.txt': 1}, state='pending')]))
        (self.source/'a.txt').write_text('modified')
        checkpoint = engine._checkpoint_path(self.root)
        self.assertIn('大小不一致', engine.validate_resume_files(checkpoint)[0])
        with patch.object(engine, '_run_proc') as process, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main_from_config(dict(resume_from=str(checkpoint))), 'partial')
        process.assert_not_called()
        self.assertEqual(engine._load_checkpoint(checkpoint)['groups'][0]['state'], 'pending')
