"""Regression gates for visible feedback and the layer card's public interaction."""
import os
from pathlib import Path
import tempfile
import unittest

_application = None


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in GUI controls')
class UiControlTests(unittest.TestCase):
    def setUp(self):
        global _application
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSettings
        from legacy_ui_baseline import MainWindow
        from sortzip_core.smoke import _MemoryCredentials
        _application = QApplication.instance() or QApplication([])
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.window = MainWindow(settings=QSettings(str(Path(temp.name)/'ui.ini'),QSettings.IniFormat),
            credential_store=_MemoryCredentials(),check_updates=False)
        self.addCleanup(self.window.close)
        self.window._switch_page(3)
        self.window.resize(900,760)
        self.window.show()
        _application.processEvents()

    def pixels(self,check):
        _application.processEvents()
        pixmap = check.grab()
        image = pixmap.toImage()
        scale = pixmap.devicePixelRatio()
        rect = check.indicator_rect()
        return [image.pixelColor(x,y).name() for y in range(int(rect.top()*scale),int((rect.bottom()+1)*scale))
                for x in range(int(rect.left()*scale),int((rect.right()+1)*scale))]

    def test_indicator_label_keyboard_and_disabled_behavior(self):
        from PySide6.QtCore import QPoint,Qt
        from PySide6.QtTest import QTest,QSignalSpy
        check = self.window.layer_stack.rows[0].volume
        spy = QSignalSpy(check.toggled)
        before = self.pixels(check)
        QTest.mouseClick(check,Qt.LeftButton,pos=check.indicator_rect().center())
        self.assertTrue(check.isChecked())
        self.assertEqual(spy.count(),1)
        selected = self.pixels(check)
        self.assertNotEqual(before,selected)
        self.assertGreater(len(set(selected)),2)  # Outline, fill, and visible mark.
        QTest.mouseClick(check,Qt.LeftButton,pos=QPoint(check.indicator_rect().right()+20,check.height()//2))
        self.assertFalse(check.isChecked())
        self.assertEqual(spy.count(),2)
        check.setFocus()
        QTest.keyClick(check,Qt.Key_Space)
        self.assertTrue(check.isChecked())
        self.assertEqual(spy.count(),3)
        check.setEnabled(False)
        QTest.mouseClick(check,Qt.LeftButton,pos=check.indicator_rect().center())
        QTest.keyClick(check,Qt.Key_Space)
        self.assertTrue(check.isChecked())
        self.assertEqual(spy.count(),3)
        self.assertNotEqual(before,self.pixels(check))

    def test_state_survives_hover_focus_repaint_resize_and_scrolling(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        from sortzip_core.nesting import Layer
        row = self.window.layer_stack.rows[0]
        check = row.volume
        for _ in range(12):
            check.click()
        self.assertFalse(check.isChecked())
        check.click()
        selected = self.pixels(check)
        QTest.mouseMove(check,QPoint(2,2))
        QTest.mouseMove(self.window,QPoint(8,8))
        row.group.setFocus()
        for _ in range(2):
            self.window.layer_stack.add_layer(Layer())
        scroll = self.window.layers_scroll.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        self.window.resize(640,600)
        scroll.setValue(0)
        self.window.resize(900,760)
        check.update()
        self.assertTrue(check.isChecked())
        self.assertEqual(selected,self.pixels(check))

    def test_caption_matches_native_checkbox_without_overlapping_indicator(self):
        from PySide6.QtWidgets import QCheckBox
        check = self.window.layer_stack.rows[0].volume
        native = QCheckBox(check.text(),check.parentWidget())
        self.addCleanup(native.deleteLater)
        native.setFont(check.font())
        native.resize(check.size())
        native.show()
        check.clearFocus()
        native.clearFocus()
        _application.processEvents()
        actual,expected = check.grab(),native.grab()
        scale = actual.devicePixelRatio()
        # Compare the complete native caption, including its first character.
        start = int((check.indicator_rect().right()+2)*scale)
        actual,expected = actual.toImage(),expected.toImage()
        self.assertEqual(actual.size(),expected.size())
        for y in range(actual.height()):
            for x in range(start,actual.width()):
                self.assertEqual(actual.pixelColor(x,y),expected.pixelColor(x,y))

    def test_every_compression_checkbox_uses_shared_component(self):
        from sortzip_core.ui_controls import StateCheckBox
        row = self.window.layer_stack.rows[0]
        for check in (self.window.first_cb,self.window.keep_cb,self.window.output_list_cb,
                      row.volume,row.verify,row.rename):
            self.assertIsInstance(check,StateCheckBox)

    def test_visible_state_dependencies_and_execution_fields_match(self):
        from PySide6.QtWidgets import QLineEdit
        row = self.window.layer_stack.rows[0]
        row.advanced_toggle.click()
        row.volume.click()
        row.verify.click()

        row.password.setText('password123')
        row.confirm.setText('password123')
        row.rename.click()
        row.suffix.setText('.png')
        row.show_password.trigger()
        value = self.window._build_config()['compression_layers'][0]
        self.assertTrue(value.enable_volume and value.verify)
        self.assertTrue(row.size.isEnabled() and row.suffix.isEnabled())
        self.assertEqual((value.password,value.suffix),('password123','.png'))
        self.assertEqual(row.password.echoMode(),QLineEdit.EchoMode.Normal)
        self.window._save_settings()
        import json
        saved = json.loads(self.window.settings.value('compression_layers'))[0]
        self.assertTrue(saved['enable_volume'] and saved['verify'])
        self.assertEqual(saved['suffix'],'.png')
        self.assertNotIn('password',saved)
        self.assertEqual(json.loads(self.window.settings.value('layer_password_required')),[True])
        row.format.setCurrentText('tar')
        value = self.window._build_config()['compression_layers'][0]
        self.assertFalse(value.enable_volume)
        self.assertEqual(value.password,'')
        self.assertFalse(row.password.isEnabled())
        row.format.setCurrentText('zip')
        self.assertEqual(self.window._build_config()['compression_layers'][0].password,'password123')
        self.window.first_cb.click()
        self.assertFalse(self.window.layer_stack.isEnabled())
        self.assertNotIn('compression_layers',self.window._build_config())

    def test_password_eyes_are_trailing_synced_keyboard_accessible_and_disabled_with_format(self):
        from PySide6.QtWidgets import QToolButton,QLineEdit
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest,QSignalSpy
        row = self.window.layer_stack.rows[0]
        row.advanced_toggle.setChecked(True)

        row.password.setText('session-test-123')
        row.confirm.setText('session-test-123')
        _application.processEvents()
        buttons = [edit.findChild(QToolButton) for edit in (row.password,row.confirm)]
        self.assertTrue(all(button is not None for button in buttons))
        spy = QSignalSpy(row.show_password.toggled)
        QTest.mouseClick(buttons[0],Qt.MouseButton.LeftButton)
        self.assertEqual(spy.count(),1)
        self.assertTrue(row.show_password.isChecked())
        self.assertTrue(all(edit.echoMode()==QLineEdit.EchoMode.Normal for edit in (row.password,row.confirm)))
        buttons[1].setFocus()
        QTest.keyClick(buttons[1],Qt.Key.Key_Space)
        self.assertEqual(spy.count(),2)

        self.assertFalse(row.show_password.isChecked())
        self.assertEqual(buttons[0].accessibleName(),'显示密码')
        for edit,button in zip((row.password,row.confirm),buttons):
            self.assertGreater(button.geometry().center().x(),edit.width()/2)
            self.assertLessEqual(button.geometry().right(),edit.width())
            self.assertEqual(edit.text(),'session-test-123')
        row.format.setCurrentText('tar')
        self.assertFalse(row.show_password.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in buttons))
        row.show_password.trigger()
        self.assertEqual(spy.count(),2)

    def test_empty_password_has_inline_hint_no_enable_checkbox_and_immediate_editing(self):
        from PySide6.QtWidgets import QCheckBox
        row = self.window.layer_stack.rows[0]
        self.assertFalse(hasattr(row,'encrypt'))
        self.assertNotIn('启用本层密码',[check.text() for check in row.findChildren(QCheckBox)])
        self.assertTrue(row.password.isEnabled())
        self.assertIn('密码留空',row.password.placeholderText())
        row.password.setText('password123')
        row.confirm.setText('password123')
        self.assertEqual(row.value().password,'password123')
        row.password.clear()
        self.assertEqual(row.value().password,'')

    def test_header_and_compact_geometry_and_wrap_preserve_controls(self):
        from PySide6.QtCore import QPoint
        row = self.window.layer_stack.rows[0]
        self.assertEqual(row.title(),'')
        self.assertEqual(row.layer_title.text(),'第 1 层')
        self.assertEqual(row.accessibleName(),'第 1 层，最外层')
        self.assertGreaterEqual(row.header.geometry().top(),16)
        self.assertEqual(row.format_label.text(),'压缩格式')
        self.assertEqual((row.group.width(),row.format.width(),row.group.height(),row.format.height()),(100,128,32,32))
        def pos(widget):
            return widget.mapTo(row,QPoint())
        self.assertEqual(pos(row.group).x()-pos(row.group_label).x()-row.group_label.width(),8)
        self.assertEqual(pos(row.format).x()-pos(row.format_label).x()-row.format_label.width(),8)
        self.assertEqual(pos(row.format_label).x()-pos(row.group).x()-row.group.width(),24)
        before = (id(row.group),id(row.format))
        # Exercise wrapping below the full row width independently of the main window's minimum.
        row.basic_fields.setFixedWidth(260)
        _application.processEvents()
        self.assertGreater(pos(row.format).y(),pos(row.group).y())
        self.assertGreaterEqual(row.basic_fields.height(),72)
        row.basic_fields.setFixedWidth(440)
        _application.processEvents()
        self.assertEqual(pos(row.format).y(),pos(row.group).y())
        self.assertEqual(before,(id(row.group),id(row.format)))

    def test_small_and_regular_windows_keep_actions_and_fields_accessible(self):
        from PySide6.QtCore import QPoint
        from sortzip_core.nesting import Layer
        row = self.window.layer_stack.rows[0]
        row.advanced_toggle.click()
        for _ in range(2):
            self.window.layer_stack.add_layer(Layer())
        for width,height in ((640,600),(900,760)):
            self.window.resize(width,height)
            _application.processEvents()
            self.assertEqual((self.window.width(),self.window.height()),(width,height))
            for action in (self.window.add_layer_btn,self.window.run_btn):
                self.assertTrue(action.isVisibleTo(self.window))
                self.assertTrue(self.window.rect().contains(action.mapTo(self.window,QPoint())))
                self.assertTrue(self.window.rect().contains(action.mapTo(self.window,action.rect().bottomRight())))
            self.assertGreater(row.group_label.width(),row.group_label.fontMetrics().horizontalAdvance(row.group_label.text()))
            self.assertLessEqual(row.basic_fields.width(),row.width()-32)

    def test_layer_header_updates_and_dark_logic_remain_compatible(self):
        from sortzip_core.nesting import Layer
        stack = self.window.layer_stack
        first = stack.rows[0]
        stack.add_layer(Layer(2))
        self.assertEqual(first.layer_title.text(),'第 1 层')
        self.assertTrue(first.outer_badge.isHidden())
        self.assertEqual(stack.rows[1].accessibleName(),'第 2 层，最外层')
        stack.remove_layer(stack.rows[1])
        self.assertFalse(first.outer_badge.isHidden())
        self.window.dark_mode_cb.setChecked(True)
        before = self.pixels(first.volume)
        first.volume.click()
        self.assertTrue(first.value().enable_volume)
        self.assertNotEqual(before,self.pixels(first.volume))

    def test_compact_step_buttons_keep_native_number_editing(self):
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        spin = self.window.layer_stack.rows[0].group
        self.assertFalse(spin.minus.isEnabled())
        QTest.mouseClick(spin.plus,Qt.LeftButton)
        self.assertEqual(spin.value(),2)
        QTest.mouseClick(spin.minus,Qt.LeftButton)
        self.assertEqual(spin.value(),1)
        spin.setFocus()
        QTest.keyClick(spin,Qt.Key_Up)
        self.assertEqual(spin.value(),2)
        spin.setValue(9999)
        self.assertFalse(spin.plus.isEnabled())
        spin.setEnabled(False)
        QTest.mouseClick(spin.minus,Qt.LeftButton)
        self.assertEqual(spin.value(),9999)

    def test_password_apply_skips_unsupported_layers_and_stays_session_only(self):
        import json
        from sortzip_core.nesting import Layer
        stack = self.window.layer_stack
        source = stack.rows[0]
        self.assertFalse(source.apply_password_btn.isEnabled())
        for fmt in ('tar','7z','zip'):
            stack.add_layer(Layer(archive_format=fmt))

        source.password.setText('shared-pass123')
        self.assertFalse(source.apply_password_btn.isEnabled())
        source.confirm.setText('shared-pass123')
        self.assertTrue(source.apply_password_btn.isEnabled())
        source.apply_password_btn.click()
        values = stack.values()
        self.assertEqual([value.password for value in values],['shared-pass123','','shared-pass123','shared-pass123'])
        self.assertEqual(stack.rows[1].value().password,'')
        self.assertTrue(all(row.value().password=='shared-pass123' for row in stack.rows[2:]))
        self.assertTrue(all(row.apply_password_btn.isHidden() for row in stack.rows[1:]))
        self.window._save_settings()
        self.assertNotIn('shared-pass123',self.window.settings.value('compression_layers'))
        self.assertEqual(json.loads(self.window.settings.value('layer_password_required')),[True,False,True,True])
        stack.add_layer(Layer())
        self.assertEqual(stack.rows[-1].value().password,'')
        self.assertEqual(stack.rows[-1].password.text(),'')
        stack.remove_layer(source)
        self.assertFalse(stack.rows[0].apply_password_btn.isHidden())
        self.assertFalse(stack.rows[0].apply_password_btn.isEnabled())  # TAR is now first.

    def test_password_apply_conflict_cancel_and_confirm(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox
        from sortzip_core.nesting import Layer
        stack = self.window.layer_stack
        source = stack.rows[0]

        source.password.setText('shared-pass123')
        source.confirm.setText('shared-pass123')
        stack.add_layer(Layer(password='different-pass'))
        with patch('sortzip_core.layer_editor.QMessageBox.question',return_value=QMessageBox.No) as prompt:
            source.apply_password_btn.click()
            self.assertEqual(stack.rows[1].password.text(),'different-pass')
            self.assertNotIn('shared-pass123',str(prompt.call_args))
        with patch('sortzip_core.layer_editor.QMessageBox.question',return_value=QMessageBox.Yes):
            source.apply_password_btn.click()
            self.assertEqual(stack.rows[1].value().password,'shared-pass123')
