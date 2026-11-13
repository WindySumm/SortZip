"""Scroll visibility never changes geometry, range, or native input semantics."""
import os
import time
import unittest


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in Qt scrollbars')
class TransientScrollBarTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QListWidget
        from sortzip_core.transient_scrollbars import TransientScrollBars
        global application
        application = QApplication.instance() or QApplication([])
        self.root = QWidget()
        self.root.resize(320,240)
        self.layout = QVBoxLayout(self.root)
        self.view = QListWidget()
        self.view.addItems([f'item {i}' for i in range(150)])
        self.layout.addWidget(self.view)
        self.manager = TransientScrollBars.attach(self.root)
        self.root.show()
        application.processEvents()
        self.bar = self.view.verticalScrollBar()
        self.state = self.bar.transient_reveal
        self.addCleanup(self.root.close)

    def wait_hidden(self):
        deadline = time.monotonic()+2
        while self.state.effect.opacity()>0 and time.monotonic()<deadline:
            application.processEvents()
            time.sleep(.01)
        self.assertEqual(self.state.effect.opacity(),0)

    def test_idle_hidden_scroll_reveals_then_hides_without_geometry_change(self):
        from PySide6.QtCore import Qt
        self.assertGreater(self.bar.maximum(),0)
        self.assertEqual(self.state.effect.opacity(),0)
        rectangle = self.view.viewport().geometry()
        self.bar.setValue(5)
        self.assertEqual(self.state.effect.opacity(),1)
        self.assertFalse(self.state.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.wait_hidden()
        self.assertEqual(self.bar.value(),5)
        self.assertEqual(rectangle,self.view.viewport().geometry())
        self.assertTrue(self.state.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_scrollbar_is_overlay_and_viewport_uses_full_width(self):
        from PySide6.QtCore import Qt
        self.assertEqual(self.view.verticalScrollBarPolicy(),Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertIs(self.state.overlay.parentWidget(),self.view.viewport())
        self.assertEqual(self.view.viewport().geometry().right(),self.view.contentsRect().right())
        self.bar.setValue(10)
        self.assertEqual(self.state.overlay.value(),10)
        self.state.overlay.setValue(20)
        self.assertEqual(self.bar.value(),20)

    def test_navigation_can_scroll_without_any_visible_indicator(self):
        self.view.setProperty('hideScrollIndicator',True)
        self.state.sync()
        self.bar.setValue(20)
        self.assertEqual(self.bar.value(),20)
        self.assertFalse(self.state.overlay.isVisible())
        self.assertEqual(self.state.effect.opacity(),0)

    def test_keyboard_scroll_keeps_native_selection_and_reveals(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.view.setCurrentRow(0)
        self.view.setFocus()
        QTest.keyClick(self.view,Qt.Key.Key_End)
        application.processEvents()
        self.assertEqual(self.view.currentRow(),149)
        self.assertGreater(self.bar.value(),0)
        self.assertEqual(self.state.effect.opacity(),1)

    def test_wheel_at_boundary_reveals_but_hover_does_not(self):
        from PySide6.QtCore import QPoint,Qt,QEvent
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest
        QTest.mouseMove(self.view.viewport(),QPoint(10,10))
        self.assertEqual(self.state.effect.opacity(),0)
        point = self.view.viewport().rect().center()
        wheel = QWheelEvent(point,self.view.viewport().mapToGlobal(point),QPoint(),QPoint(0,120),
            Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
        application.sendEvent(self.view.viewport(),wheel)
        self.assertEqual(self.bar.value(),0)
        self.assertEqual(self.state.effect.opacity(),1)

    def test_drag_hold_prevents_fade_and_release_restarts_delay(self):
        self.bar.setValue(5)
        self.state.overlay.setSliderDown(True)
        self.state.fade()
        self.assertEqual(self.state.effect.opacity(),1)
        self.state.overlay.setSliderPosition(20)
        self.assertEqual(self.bar.value(),20)
        self.state.overlay.setSliderDown(False)
        self.wait_hidden()

    def test_new_dialog_views_install_once_and_horizontal_is_unchanged(self):
        from PySide6.QtWidgets import QDialog,QVBoxLayout,QTextEdit,QTableWidget,QTreeWidget,QScrollArea
        dialog = QDialog(self.root)
        from sortzip_core.transient_scrollbars import TransientScrollBars
        self.assertIs(TransientScrollBars.attach(dialog),self.manager)
        layout = QVBoxLayout(dialog)
        views = [QTextEdit(),QTableWidget(),QTreeWidget(),QScrollArea()]
        for view in views:
            layout.addWidget(view)
        dialog.show()
        application.processEvents()
        try:
            for view in views:
                controller = view.verticalScrollBar().transient_reveal
                self.assertIs(controller,self.manager.install(view))
                self.assertIsNone(view.horizontalScrollBar().graphicsEffect())
            self.bar.setValue(5)
            self.view.clear()
            application.processEvents()
            self.assertEqual(self.state.effect.opacity(),0)
        finally:
            dialog.close()
