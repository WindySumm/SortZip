"""Isolated replacements for local UI scripts; opt-in, without network or real settings."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_application = None


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION') == '1', 'opt-in GUI regressions')
class GuiRegressions(unittest.TestCase):
    def setUp(self):
        global _application
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from legacy_ui_baseline import MainWindow
        from sortzip_core.smoke import _MemoryCredentials
        _application = QApplication.instance() or QApplication([])
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.window = MainWindow(settings=QSettings(str(self.root/'gui.ini'), QSettings.Format.IniFormat),
                                 credential_store=_MemoryCredentials(), check_updates=False)
        self.addCleanup(self.window.close)

    def test_password_dialog_validates_actual_accept_slot(self):
        from PySide6.QtWidgets import QDialog
        from sortzip_core.widgets import PasswordDialog
        dialog = PasswordDialog()
        dialog.password_edit.setText('12345678')
        dialog.confirm_edit.setText('different')
        with patch('sortzip_core.widgets.show_styled_dialog') as warning:
            dialog._on_ok()
        warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.confirm_edit.setText('12345678')
        dialog._on_ok()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_unified_inline_layer_ui_has_no_duplicate_visible_mode_controls(self):
        window = self.window
        window._switch_page(3)
        window.show()
        _application.processEvents()
        self.assertEqual(len(window.layer_stack.rows),1)
        self.assertTrue(window.layer_stack.rows[0].isVisible())
        self.assertFalse(window.double_cb.isVisible())
        self.assertFalse(window.nested_cb.isVisible())
        self.assertFalse(window.layers_btn.isVisible())
        self.assertFalse(window.password_group.isVisible())
        self.assertEqual(window.first_cb.text(),'启用压缩')
        window.add_layer_btn.click()
        self.assertEqual(len(window.layer_stack.rows),2)
        self.assertEqual(len(window._build_config()['compression_layers']),2)
        window._set_task_active(True)
        self.assertFalse(window.add_layer_btn.isEnabled())
        self.assertFalse(window.layer_stack.isEnabled())
        window._set_task_active(False)

    def test_unified_outer_suffix_moves_when_layers_added_or_removed(self):
        from sortzip_core.nesting import Layer
        stack = self.window.layer_stack
        row = stack.rows[0]
        row.rename.setChecked(True)
        row.suffix.setText('.png')
        stack.add_layer(Layer(2))
        self.assertIsNone(stack.values()[0].suffix)
        self.assertEqual(stack.values()[-1].suffix,'.png')
        stack.remove_layer(stack.rows[-1])
        self.assertEqual(stack.values()[0].suffix,'.png')

    def test_legacy_settings_migrate_into_visible_cards(self):
        from legacy_ui_baseline import MainWindow
        from sortzip_core.smoke import _MemoryCredentials
        from PySide6.QtCore import QSettings
        settings = QSettings(str(self.root/'legacy.ini'),QSettings.Format.IniFormat)
        for key,value in dict(first_compress=True,double_compress=True,group_size=4,
            enable_volume=True,archive_rename_enable=True,archive_suffix='.zipp').items():
            settings.setValue(key,value)
        other = MainWindow(settings=settings,credential_store=_MemoryCredentials(),check_updates=False)
        self.addCleanup(other.close)
        self.assertEqual([layer.group_size for layer in other._build_config()['compression_layers']],[4,1])
        self.assertEqual(other.layer_stack.rows[-1].suffix.text(),'.zipp')
        other._save_settings()
        self.assertTrue(settings.value('unified_layers',False,type=bool))

    def test_run_uses_inline_layers_without_opening_an_editor(self):
        from sortzip_core.nesting import Layer
        window = self.window
        source = self.root/'run-source'
        source.mkdir()
        for name in ('a.txt','b.txt'):
            (source/name).write_text(name)
        window.src_edit.setText(str(source))
        window.dest_edit.setText(str(self.root/'run-out'))
        window._enable_all_mappings()
        window.layer_stack.rows[0].format.setCurrentText('7z')
        window.layer_stack.add_layer(Layer(2))
        window.confirm_config_cb.setChecked(False)
        with patch.object(window,'_launch_worker') as launch, patch('legacy_ui_baseline.LayerEditor') as popup:
            window._run()
        popup.assert_not_called()
        config = launch.call_args.args[0]
        self.assertEqual([layer.archive_format for layer in config['compression_layers']],['7z','zip'])
        self.assertEqual(len(config['_nested_plan'].tasks),3)

    def test_layer_editor_outer_lock_copy_delete_and_format_preferences(self):
        from sortzip_core.layer_editor import LayerEditor
        from sortzip_core.nesting import Layer
        editor = LayerEditor((Layer(4,enable_volume=True,password='password123'),Layer(suffix='.png')))
        self.addCleanup(editor.close)
        self.assertFalse(editor.rows[0].rename.isEnabled())
        self.assertTrue(editor.rows[-1].rename.isEnabled())
        row = editor.rows[0]
        row.format.setCurrentText('gz')
        self.assertFalse(row.password.isEnabled())
        self.assertFalse(row.volume.isEnabled())
        self.assertEqual(row.group.value(),1)
        row.format.setCurrentText('7z')
        self.assertEqual(row.group.value(),4)
        self.assertEqual(row.value().password,'password123')
        self.assertTrue(row.value().enable_volume)
        editor.copy_layer(row)
        self.assertEqual(len(editor.rows),3)
        self.assertFalse(editor.rows[1].rename.isEnabled())
        editor.remove_layer(editor.rows[-1])
        self.assertTrue(editor.rows[-1].rename.isEnabled())

    def test_layer_editor_rejects_password_mismatch(self):
        from PySide6.QtWidgets import QDialog
        from sortzip_core.layer_editor import LayerEditor
        from sortzip_core.nesting import Layer
        editor = LayerEditor((Layer(password='password123'),))
        self.addCleanup(editor.close)
        editor.rows[0].confirm.setText('different')
        with patch('sortzip_core.layer_editor.show_styled_dialog') as warning:
            editor.save()
        warning.assert_called_once()
        self.assertNotEqual(editor.result(),QDialog.DialogCode.Accepted)

    def test_nested_mode_locks_legacy_controls_and_empty_password_disables_encryption(self):
        import json
        from sortzip_core.nesting import Layer
        window = self.window
        window.layer_stack.set_layers((Layer(password='private-password'),Layer(2,suffix='.png')))
        window.nested_cb.setChecked(True)
        self.assertFalse(window.double_cb.isEnabled())
        self.assertFalse(window.password_group.isEnabled())
        self.assertTrue(window.layers_btn.isEnabled())
        self.assertEqual(len(window._build_config()['compression_layers']),2)
        window._save_settings()
        self.assertNotIn('private-password',window.settings.value('compression_layers'))
        self.assertEqual(json.loads(window.settings.value('layer_password_required')),[True,False])
        window.layer_stack.rows[0].password.clear()
        window.layer_stack.rows[0].confirm.clear()
        self.assertEqual(window._build_config()['compression_layers'][0].password,'')
        window._set_task_active(True)
        self.assertFalse(window.nested_cb.isEnabled())
        self.assertFalse(window.layers_btn.isEnabled())
        window._save_settings()
        self.assertEqual(json.loads(window.settings.value('layer_password_required')),[False,False])
        window._set_task_active(False)

    def test_multilayer_resume_import_collects_password_and_launches(self):
        from PySide6.QtWidgets import QDialog
        from sortzip_core import engine
        from sortzip_core.nesting import Layer,Unit,MaterializedUnit,plan_layers
        from sortzip_core.nested_execution import execute_plan
        from sortzip_core.widgets import PasswordDialog
        root = self.root/'nested'
        root.mkdir()
        source = root/'a.txt'
        source.write_text('a')
        plan = plan_layers([Unit('a','doc',('a',))],[Layer(password='password123')])
        execute_plan(plan,{'a':MaterializedUnit((str(source),),True)},root,cancel_check=lambda:True)
        checkpoint = engine._checkpoint_path(root)
        with patch.object(PasswordDialog,'exec',return_value=QDialog.DialogCode.Accepted), \
                patch.object(PasswordDialog,'password',return_value='password123'), \
                patch.object(self.window,'_confirm_nested_resume',return_value=True), \
                patch.object(self.window,'_launch_worker') as launch:
            self.window._start_resume(str(checkpoint))
        self.assertEqual(launch.call_args.args[0]['layer_passwords'],{0:'password123'})

    def test_invalid_multilayer_resume_does_not_prompt_for_password(self):
        import json
        checkpoint = self.root/'.sortzip_resume.json'
        checkpoint.write_text(json.dumps(dict(kind='multilayer',schema=2,tasks=[])))
        with patch('legacy_ui_baseline.PasswordDialog') as password, \
                patch('legacy_ui_baseline.show_styled_dialog') as warning:
            self.window._start_resume(str(checkpoint))
        password.assert_not_called()
        warning.assert_called_once()

    def test_nested_password_preference_reloads_without_secret(self):
        from legacy_ui_baseline import MainWindow
        from sortzip_core.nesting import Layer
        from sortzip_core.smoke import _MemoryCredentials
        self.window.layer_stack.set_layers((Layer(password='private-password'),))
        self.window.nested_cb.setChecked(True)
        self.window._save_settings()
        other = MainWindow(settings=self.window.settings,credential_store=_MemoryCredentials(),check_updates=False)
        self.addCleanup(other.close)
        self.assertEqual(other._compression_layers[0].password,'')
        self.assertEqual(other._missing_layer_passwords,{0})
        with patch('legacy_ui_baseline.show_styled_dialog') as warning, patch.object(other,'_launch_worker') as launch:
            other._run()
        warning.assert_called_once()
        launch.assert_not_called()

    def test_nested_preview_displays_actual_outer_archive_path(self):
        from sortzip_core.nesting import Layer
        source = self.root/'preview-source'
        source.mkdir()
        for name in ('a.txt','b.txt'):
            (source/name).write_text(name)
        config = dict(src=str(source),dest=str(self.root/'preview-out'),group_size=1,
            custom_names={'.txt':'doc'},compression_layers=(Layer(1),Layer(2,suffix='.png')))
        with patch.object(self.window,'_build_config',return_value=config):
            self.window._refresh_preview()
        self.assertEqual(self.window.preview_table.rowCount(),2)
        self.assertEqual(self.window.preview_table.item(0,2).text(),'layer-2/doc/1-2/1-2.png')

    def test_map_all_excludes_checkpoint_and_navigation_has_five_pages(self):
        from sortzip_core.constants import RESUME_FILE_NAME
        source = self.root/'src'
        source.mkdir()
        (source/'a.txt').write_text('sample')
        (source/RESUME_FILE_NAME).write_text('{}')
        self.window.src_edit.setText(str(source))
        self.window._enable_all_mappings()
        mappings = self.window._build_config()['custom_names']
        self.assertIn('.txt', mappings)
        self.assertNotIn('.json', mappings)
        for index in range(5):
            self.window._switch_page(index)
            self.assertEqual(self.window.stack.currentIndex(), index)

    def make_checkpoint(self, missing=False):
        from sortzip_core import engine
        folder = self.root/'out/doc'
        folder.mkdir(parents=True)
        if not missing:
            (folder/'a.txt').write_text('a')
        engine._write_checkpoint(folder.parent, dict(config=dict(group_size=1, password=False, enable_volume=False),
            groups=[dict(folder='doc', base_name='1', files=['a.txt'], sizes={'a.txt': 1}, state='pending')]))
        return engine._checkpoint_path(folder.parent)

    def test_resume_import_launches_confirmed_config(self):
        from PySide6.QtWidgets import QDialog
        from sortzip_core.widgets import PasswordDialog
        checkpoint = self.make_checkpoint()
        with patch('legacy_ui_baseline.QFileDialog.getOpenFileName', return_value=(str(checkpoint), '')), \
                patch.object(self.window, '_confirm_resume', return_value=True), \
                patch.object(PasswordDialog, 'exec', return_value=QDialog.DialogCode.Accepted), \
                patch.object(self.window, '_launch_worker') as launch:
            self.window._import_resume()
        self.assertEqual(launch.call_args.args[0]['resume_from'], str(checkpoint))
        self.assertEqual(launch.call_args.args[0]['dest'], str(checkpoint.parent))

    def test_resume_warning_can_abort_without_starting_worker(self):
        from PySide6.QtWidgets import QDialog
        checkpoint = self.make_checkpoint(missing=True)
        with patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Rejected), \
                patch.object(self.window, '_launch_worker') as launch:
            self.window._start_resume(str(checkpoint))
        launch.assert_not_called()

    def test_finished_status_and_thread_reference_cleanup(self):
        self.window.worker = Mock(status='safe_cancelled', stats={'groups': 2, 'groups_total': 3})
        with patch('legacy_ui_baseline.show_stats_dialog') as report:
            self.window._on_finished()
            report.assert_not_called()
            self.window._on_thread_finished()
        self.assertEqual(report.call_args.kwargs['status'], 'safe_cancelled')
        self.assertIsNone(self.window.worker)
        self.assertIsNone(self.window.thread)

    def test_thread_factory_runs_task_and_preserves_compatibility_import(self):
        from PySide6.QtCore import QEventLoop, QTimer
        from sortzip_core.tasks import Worker, create_worker_thread
        from sortzip_core.widgets import Worker as LegacyWorker
        self.assertIs(Worker, LegacyWorker)
        source = self.root/'src'
        source.mkdir()
        (source/'a.txt').write_text('task')
        thread, worker = create_worker_thread(dict(src=str(source), dest=str(self.root/'out'),
            custom_names={'.txt': 'doc'}, group_size=1, first_compress=False))
        loop, timer, result = QEventLoop(), QTimer(), {}
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        worker.finished.connect(lambda: result.update(status=worker.status, stats=dict(worker.stats)))
        thread.finished.connect(loop.quit)
        timer.start(5000)
        thread.start()
        loop.exec()
        timer.stop()
        if thread.isRunning():
            worker.force_cancel()
            thread.quit()
            thread.wait(2000)
        self.assertFalse(thread.isRunning())
        thread.deleteLater()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['stats']['files_moved'], 1)

    def test_worker_failure_restores_stdout_and_redacts_error(self):
        import sys
        from sortzip_core.tasks import Worker
        worker = Worker({'password': 'test-secret-123'})
        messages, finished = [], []
        worker.error.connect(messages.append)
        worker.finished.connect(lambda: finished.append(True))
        previous = sys.stdout
        with patch('sortzip_core.tasks.main_from_config', side_effect=RuntimeError('failed test-secret-123')):
            worker.run()
        self.assertIs(sys.stdout, previous)
        self.assertEqual(worker.status, 'error')
        self.assertEqual(finished, [True])
        self.assertNotIn('test-secret-123', messages[0])

    def test_busy_state_blocks_all_task_entry_points(self):
        self.window.thread = Mock()
        self.window.worker = Mock()
        with patch('legacy_ui_baseline.create_worker_thread') as factory, \
                patch('legacy_ui_baseline._load_checkpoint') as load, \
                patch.object(self.window, '_build_config') as build:
            self.assertFalse(self.window._launch_worker({}))
            self.window._start_resume('not-used.json')
            self.window._run()
        factory.assert_not_called()
        load.assert_not_called()
        build.assert_not_called()
        self.window.thread = None
        self.window.worker = None

    def test_result_is_presented_only_after_thread_references_are_cleared(self):
        self.window._set_task_active(True)
        self.window.worker = Mock(status='completed', stats={'files_moved': 3})
        self.window.thread = Mock()
        observations = []
        def present(*args, **kwargs):
            observations.append((self.window.worker, self.window.thread, self.window.run_btn.isEnabled()))
        with patch('legacy_ui_baseline.show_stats_dialog', side_effect=present):
            self.window._on_finished()
            self.assertFalse(self.window.run_btn.isEnabled())
            self.assertEqual(observations, [])
            self.window._on_thread_finished()
        self.assertEqual(observations, [(None, None, True)])

    def test_close_waits_for_running_worker_and_skips_completion_modal(self):
        import threading
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication
        started = threading.Event()
        def slow_task(config, cancel_check=None, **kwargs):
            started.set()
            for _ in range(200):
                if cancel_check():
                    return 'safe_cancelled'
                threading.Event().wait(0.01)
            return 'completed'
        self.window.show()
        loop, timeout, poll = QEventLoop(), QTimer(), QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        requested = []
        def check():
            if started.is_set() and not requested:
                requested.append(True)
                self.window.close()
                self.assertTrue(self.window.isVisible())
            if requested and self.window.thread is None:
                loop.quit()
        poll.timeout.connect(check)
        with patch('sortzip_core.tasks.main_from_config', side_effect=slow_task), \
                patch('legacy_ui_baseline.show_stats_dialog') as report:
            self.window._launch_worker({})
            timeout.start(5000)
            poll.start(10)
            loop.exec()
            timeout.stop()
            poll.stop()
            QApplication.processEvents()
            self.assertTrue(requested)
            self.assertIsNone(self.window.thread)
            self.assertIsNone(self.window.worker)
            self.assertFalse(self.window.isVisible())
            report.assert_not_called()

    def test_two_real_tasks_can_run_sequentially_in_one_window(self):
        from PySide6.QtCore import QEventLoop, QTimer
        source = self.root/'source'
        source.mkdir()
        (source/'a.txt').write_text('sequence')
        for index in (1, 2):
            loop, timer = QEventLoop(), QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            with patch('legacy_ui_baseline.show_stats_dialog', side_effect=lambda *a, **k: loop.quit()) as report:
                self.window._launch_worker(dict(src=str(source), dest=str(self.root/f'output-{index}'),
                    custom_names={'.txt': 'doc'}, first_compress=False, keep_files=True, group_size=1))
                timer.start(5000)
                loop.exec()
                timer.stop()
                self.assertIsNone(self.window.thread)
                self.assertIsNone(self.window.worker)
                self.assertTrue(self.window.run_btn.isEnabled())
                report.assert_called_once()
                self.assertEqual(report.call_args.kwargs['status'], 'completed')
            self.assertEqual((self.root/f'output-{index}/doc/a.txt').read_text(), 'sequence')

    def test_worker_factory_failure_unlocks_configuration(self):
        with patch('legacy_ui_baseline.create_worker_thread', side_effect=RuntimeError('cannot create worker')):
            self.assertFalse(self.window._launch_worker({}))
        self.assertIsNone(self.window.worker)
        self.assertIsNone(self.window.thread)
        self.assertTrue(self.window.run_btn.isEnabled())
        self.assertTrue(self.window.stack.widget(0).isEnabled())
