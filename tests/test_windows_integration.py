"""Opt-in local integration: SORTZIP_INTEGRATION=1 python -m unittest discover -s tests.

Only generated files and a unique test credential target are touched.
"""
import os
import shutil
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.credentials import WindowsCredentialStore, load_password, save_password


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION') == '1', 'opt-in Windows integration')
class WindowsIntegration(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_real_bandizip_password_volume_double_and_disguised_suffix(self):
        bz = shutil.which('bz')
        if not bz:
            self.skipTest('bz not installed')
        for volume, double in [(False, False), (False, True), (True, True)]:
            with self.subTest(volume=volume, double=double):
                output = self.root / f'{volume}-{double}'
                folder = output / 'doc'
                folder.mkdir(parents=True)
                (folder / 'a.txt').write_text('sample')
                (folder / 'b.bin').write_bytes(os.urandom(6000))
                result = engine.group_compress(output, 2, 'test-pass-123', bandizip_path=bz,
                    verify=True, enable_volume=volume, volume_size='1k' if volume else None,
                    double_compress=double, first_suffix='-First' if double else '',
                    first_suffix_replace=None if volume else '.disguised')
                self.assertEqual(result, 'completed')
                final = folder / ('1-2.zipp' if double else '1-2.disguised')
                self.assertTrue(zipfile.is_zipfile(final))
                self.assertEqual(list(folder.iterdir()), [final])

    def test_real_credential_roundtrip_with_isolated_settings(self):
        from PySide6.QtCore import QSettings
        store = WindowsCredentialStore('SortZip/Test/' + uuid.uuid4().hex)
        self.addCleanup(store.delete)
        settings = QSettings(str(self.root / 'test.ini'), QSettings.Format.IniFormat)
        warnings = []
        settings.setValue('remember_pwd', True)
        settings.setValue('password', '测试旧密码123')
        self.assertEqual(load_password(settings, store, warnings.append), '测试旧密码123')
        self.assertFalse(settings.contains('password'))
        self.assertEqual(store.read(), '测试旧密码123')
        self.assertTrue(save_password(settings, store, 'new-pass-123', True, warnings.append))
        self.assertEqual(store.read(), 'new-pass-123')
        self.assertTrue(save_password(settings, store, '', False, warnings.append))
        self.assertEqual(store.read(), '')
        self.assertFalse(warnings)

    def test_gui_settings_and_failure_status(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSettings
        import legacy_ui_baseline as gui
        application = QApplication.instance() or QApplication([])
        settings = QSettings(str(self.root / 'gui.ini'), QSettings.Format.IniFormat)
        with patch.object(gui, 'QSettings', return_value=settings), \
                patch.object(gui.UpdateChecker, 'start'), \
                patch.object(gui.WindowsCredentialStore, 'delete'), \
                patch.object(gui.WindowsCredentialStore, 'write'), \
                patch.object(gui, 'show_stats_dialog'):
            window = gui.MainWindow()
            window.password_edit.setText('test-pass-123')
            window.remember_pwd_cb.setChecked(True)
            window._save_settings()
            self.assertFalse(settings.contains('password'))
            from unittest.mock import Mock
            window.worker = Mock(stats={}, status='error')
            window.progress_bar.setValue(42)
            window._on_finished()
            self.assertEqual(window.progress_bar.value(), 42)
            self.assertIn('失败', window.progress_bar.format())
            window.worker = None
            window.close()
            application.processEvents()

    def test_gui_preview_relative_paths_and_worker_stats(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSettings, Qt
        import legacy_ui_baseline as gui
        from sortzip_core.widgets import Worker
        application = QApplication.instance() or QApplication([])
        settings = QSettings(str(self.root/'preview.ini'), QSettings.Format.IniFormat)
        src = self.root/'src'
        for name in ['a/same.txt', 'b/same.txt']:
            file = src/name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(name)
        config = dict(src=str(src), dest=str(self.root/'out'), custom_names={'.txt': 'doc'},
            recursive=True, group_size=2, first_compress=True, double_compress=False, archive_format='7z',
            first_suffix_replace='.png', enable_volume=False, naming_rules=[{'match_folder': '*', 'template': '{n}{ext}'}])
        with patch.object(gui, 'QSettings', return_value=settings), \
                patch.object(gui.UpdateChecker, 'start'), patch.object(gui, 'WindowsCredentialStore'):
            window = gui.MainWindow()
            self.assertEqual([window.compress_format_combo.itemText(i) for i in range(window.compress_format_combo.count())],
                             ['zip', '7z', 'tar', 'gz', 'xz', 'lzh'])
            with patch.object(window, '_build_config', side_effect=lambda: dict(config, preview_order=window._preview_order)):
                window._refresh_preview()
                self.assertEqual(window.preview_table.columnCount(), 3)
                self.assertEqual(window.preview_table.item(0, 0).data(Qt.ItemDataRole.UserRole), 'a/same.txt')
                self.assertEqual(window.preview_table.item(0, 2).text(), '1-2.png')
                for col in range(3):
                    first, second = window.preview_table.takeItem(0, col), window.preview_table.takeItem(1, col)
                    window.preview_table.setItem(0, col, second)
                    window.preview_table.setItem(1, col, first)
                window._on_preview_reordered()
                self.assertEqual(window._preview_order['doc'], ['b/same.txt', 'a/same.txt'])
                application.processEvents()
                window._refresh_preview()
                self.assertEqual(window.preview_table.item(0, 0).text(), 'b/same.txt')
                config['keep_hierarchy'] = True
                window._refresh_preview()
                self.assertEqual([window.preview_tab_bar.tabText(i) for i in range(window.preview_tab_bar.count())],
                                 ['doc/a', 'doc/b'])
            window.close()
            application.processEvents()
        worker = Worker({})
        worker.stats = {'groups': 0}
        values = []
        worker.progress.connect(lambda value, maximum, message: values.append(value))
        worker._report_progress(40, 100, 1, 1, '压缩 (1/1)')
        self.assertEqual(worker.stats['groups'], 0)
        self.assertEqual(values[-1], 99)

    def test_format_controls_lock_and_restore(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSettings
        import legacy_ui_baseline as gui
        application = QApplication.instance() or QApplication([])
        settings = QSettings(str(self.root/'locks.ini'), QSettings.Format.IniFormat)
        with patch.object(gui, 'QSettings', return_value=settings), \
                patch.object(gui.UpdateChecker, 'start'), patch.object(gui, 'WindowsCredentialStore'):
            window = gui.MainWindow()
            window.first_cb.setChecked(True)
            row = window.layer_stack.rows[0]
            row.format.setCurrentText('zip')

            row.password.setText('preserved-password')
            row.confirm.setText('preserved-password')
            row.group.setValue(8)
            row.volume.setChecked(True)
            for fmt in ('tar', 'lzh', 'gz', 'xz'):
                row.format.setCurrentText(fmt)
                self.assertFalse(row.password.isEnabled())
                self.assertFalse(row.confirm.isEnabled())
                self.assertFalse(row.show_password.isEnabled())
                self.assertFalse(row.volume.isEnabled())
                self.assertFalse(row.size.isEnabled())
                self.assertEqual(window._build_config()['compression_layers'][0].password, '')
                if fmt in ('gz', 'xz'):
                    self.assertFalse(row.group.isEnabled())
                    self.assertEqual(row.group.value(), 1)
            window._save_settings()
            import json
            preferences = json.loads(settings.value('layer_format_preferences'))
            self.assertEqual(preferences[0]['group'],8)
            self.assertTrue(preferences[0]['volume'])
            row.format.setCurrentText('7z')
            self.assertTrue(row.password.isEnabled())
            self.assertEqual(row.password.text(), 'preserved-password')
            self.assertEqual(window._build_config()['compression_layers'][0].password, 'preserved-password')
            self.assertTrue(row.volume.isChecked())
            self.assertTrue(row.size.isEnabled())
            self.assertEqual(row.group.value(), 8)
            row.rename.setChecked(True)
            self.assertTrue(row.suffix.isEnabled())
            window.first_cb.setChecked(False)
            self.assertFalse(row.password.isEnabled())
            self.assertFalse(row.verify.isEnabled())
            window.close()
            application.processEvents()
