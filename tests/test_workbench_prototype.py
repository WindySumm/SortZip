"""Review prototype gates; real execution stays behind the layout review boundary."""
import os
import unittest


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in Qt prototype')
class WorkbenchPrototypeTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from sortzip_core.workbench_prototype import WorkbenchPrototype
        global _application
        _application = QApplication.instance() or QApplication([])
        self.window = WorkbenchPrototype()
        self.addCleanup(self.window.close)
        self.window.show()
        _application.processEvents()

    def test_context_switch_keeps_invalid_drafts_and_one_editor(self):
        window = self.window
        first,second = window.state.layers
        editor = window.layer_editor
        window.navigate(first.key)

        editor.password.setText('unfinished')
        editor.confirm.setText('different')
        editor.template.setText('{folder}_{n}')
        editor.advanced_toggle.setChecked(True)
        window.navigate(second.key)
        editor.template.setText('outer_{n}')
        window.navigate(first.key)
        self.assertIs(window.layer_editor,editor)
        self.assertEqual(editor.password.text(),'unfinished')
        self.assertEqual(editor.confirm.text(),'different')
        self.assertEqual(editor.template.text(),'{folder}_{n}')
        self.assertTrue(editor.advanced_toggle.isChecked())
        window.refresh()
        self.assertIn('密码',window.preview_status.text())
        self.assertGreater(window.tree.topLevelItemCount(),0)
        self.assertFalse(window.review_btn.isEnabled())
        for _ in range(10):
            window.navigate(second.key)
            window.navigate(first.key)
        before = window.state.revision
        editor.template.setText('changed_{n}')
        self.assertEqual(window.state.revision,before+1)

    def test_layer_add_copy_remove_and_outer_suffix_transfer(self):
        window = self.window
        window.navigate(window.state.layers[-1].key)
        window.layer_editor.rename.setChecked(True)
        window.layer_editor.suffix.setText('.jpg')
        window.layer_editor.template.setText('outer_{n}')
        window.copy_layer()
        self.assertEqual(len(window.state.layers),3)
        self.assertEqual(window.draft().data['template'],'outer_{n}')
        self.assertEqual(window.layer_editor.template.text(),'outer_{n}')
        self.assertTrue(window.state.layers[-1].data['rename'])
        self.assertFalse(window.state.layers[-2].data['rename'])
        window.remove_layer()
        self.assertEqual(len(window.state.layers),2)
        self.assertTrue(window.draft().data['rename'])
        self.assertEqual(window.layer_editor.suffix.text(),'.jpg')
        window.remove_layer()
        self.assertFalse(window.remove_btn.isEnabled())
        window.remove_layer()
        self.assertEqual(len(window.state.layers),1)

    def test_preview_has_real_layer_plan_and_can_locate_previous_member(self):
        window = self.window
        first,second = window.state.layers
        window.navigate(second.key)
        entries,plan,warnings = window.state.structure()
        self.assertEqual(len(entries),6)
        self.assertEqual(len(plan.final_ids),2)
        self.assertFalse(warnings)
        self.assertEqual(window.tree.topLevelItemCount(),2)
        child = window.tree.topLevelItem(0).child(0)
        self.assertIn('.zip',child.text(0))
        window.tree.setCurrentItem(child)
        window.locate_source()
        self.assertEqual(window.state.selected,first.key)
        window.navigate('final')
        self.assertIn('示例输出',window.summary.text())

    def test_invalid_names_clear_preview_and_cannot_review(self):
        window = self.window
        window.navigate(window.state.layers[0].key)
        window.layer_editor.template.setText('same')
        window.refresh()
        self.assertIn('重复',window.preview_status.text())
        self.assertEqual(window.tree.topLevelItemCount(),0)
        self.assertFalse(window.review_btn.isEnabled())
        window.layer_editor.template.setText('fixed_{n}')
        window.refresh()
        self.assertTrue(window.review_btn.isEnabled())

    def test_mapping_and_file_rules_update_all_downstream_contexts(self):
        window = self.window
        from PySide6.QtWidgets import QTableWidgetItem
        window.naming.table.setItem(0,1,QTableWidgetItem('new_{n}{ext}'))
        window.navigate(window.state.layers[0].key)
        self.assertTrue(window.tree.topLevelItem(0).child(0).text(0).startswith('new_'))
        window.navigate('mapping')
        window.mapping_table.setCurrentCell(0,0)
        window.remove_mapping()
        entries,plan,_ = window.state.structure()
        self.assertEqual(len(entries),2)
        self.assertEqual(len(plan.final_ids),1)

    def test_breakpoint_footer_and_dark_preview(self):
        from PySide6.QtCore import Qt,QPoint
        window = self.window
        for width,height in ((1280,800),(900,760),(640,600)):
            window.resize(width,height)
            _application.processEvents()
            self.assertEqual((window.width(),window.height()),(width,height))
            self.assertEqual(window.content.orientation(),Qt.Horizontal if width>=1100 else Qt.Vertical)
            self.assertTrue(window.rect().contains(window.review_btn.mapTo(window,window.review_btn.rect().bottomRight())))
        window.toggle_theme()
        window.navigate(window.state.layers[0].key)
        window.layer_editor.volume.click()
        self.assertTrue(window.draft().data['volume'])

    def test_password_apply_session_only_and_skip_tar(self):
        window = self.window
        first,second = window.state.layers
        window.navigate(first.key)
        row = window.layer_editor

        row.password.setText('shared-demo123')
        row.confirm.setText('shared-demo123')
        row.apply_password_btn.click()
        self.assertEqual(second.data['password'],'shared-demo123')
        window.navigate(second.key)
        self.assertTrue(row.password.text())
        row.format.setCurrentText('tar')
        self.assertFalse(row.password.isEnabled())
        window.navigate(first.key)
        self.assertFalse(row.apply_password_btn.isEnabled())

    def test_single_stream_preference_survives_context_switch(self):
        window = self.window
        first,second = window.state.layers
        window.navigate(first.key)
        row = window.layer_editor
        row.group.setValue(7)
        row.format.setCurrentText('gz')
        self.assertEqual(row.group.value(),1)
        window.navigate(second.key)
        window.navigate(first.key)
        self.assertEqual(row.group.value(),1)
        row.format.setCurrentText('zip')
        self.assertEqual(row.group.value(),7)
        self.assertEqual(first.data['group'],7)

    def test_navigation_focus_does_not_add_inner_frame(self):
        from PySide6.QtCore import QRect,Qt
        from PySide6.QtGui import QImage,QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem,QStyle
        navigation = self.window.navigation
        option = QStyleOptionViewItem()
        option.initFrom(navigation)
        option.rect = QRect(0,0,180,64)
        option.widget = navigation
        option.state |= QStyle.State_Selected|QStyle.State_Active
        images = []
        for focused in (False,True):
            current = QStyleOptionViewItem(option)
            if focused:
                current.state |= QStyle.State_HasFocus
            else:
                current.state &= ~QStyle.State_HasFocus
            image = QImage(180,64,QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            navigation.itemDelegate().paint(painter,current,navigation.model().index(0,0))
            painter.end()
            images.append(image)
        self.assertEqual(images[0],images[1])

    def test_navigation_keeps_keyboard_operation(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window = self.window
        window.navigation.setFocus()
        QTest.keyClick(window.navigation,Qt.Key_Down)
        self.assertEqual(window.state.selected,'naming')
        QTest.keyClick(window.navigation,Qt.Key_Down)
        self.assertEqual(window.state.selected,window.state.layers[0].key)

    def test_filename_preview_updates_while_rule_editor_is_still_open(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLineEdit,QApplication
        from PySide6.QtTest import QTest
        window = self.window
        window.navigate('naming')
        self.assertEqual(window.preview_tabs.currentIndex(),1)
        self.assertIn('原文件名',window.preview_title.text())
        table = window.naming.table
        table.setCurrentCell(0,1)
        table.editItem(table.item(0,1))
        _application.processEvents()
        editor = QApplication.focusWidget()
        self.assertIsInstance(editor,QLineEdit)
        QTest.keyClick(editor,Qt.Key_A,Qt.ControlModifier)
        QTest.keyClicks(editor,'live_{n}{ext}')
        QTest.qWait(250)
        self.assertTrue(editor.hasFocus())
        self.assertEqual(window.filename_table.item(0,1).text(),'live_1.png')
        self.assertEqual(window.filename_table.item(0,0).text(),'封面.png')
        self.assertEqual(window.filename_table.item(0,3).text(),'图片/图片_1.zip')

    def test_filename_preview_stays_available_when_archive_template_invalid(self):
        window = self.window
        window.navigate(window.state.layers[0].key)
        window.layer_editor.template.setText('duplicate')
        window.refresh()
        self.assertEqual(window.tree.topLevelItemCount(),0)
        window.preview_tabs.setCurrentIndex(1)
        self.assertEqual(window.filename_table.rowCount(),6)
        self.assertEqual(window.filename_table.item(0,1).text(),'image_1.png')
        self.assertEqual(window.filename_table.item(0,3).text(),'归档计划待修正')
        window.navigate('naming')
        window.naming.table.item(0,1).setText('same.png')
        window.refresh()
        self.assertEqual(window.filename_table.rowCount(),0)
        self.assertIn('重名',window.preview_status.text())

    def test_preview_tabs_keep_context_and_restore_source_button(self):
        window = self.window
        first,second = window.state.layers
        window.navigate(second.key)
        window.preview_tabs.setCurrentIndex(1)
        window.layer_editor.template.setText('outer_{n}')
        window.refresh()
        self.assertIn('outer_1.zip',window.filename_table.item(0,3).text())
        window.preview_tabs.setCurrentIndex(0)
        self.assertTrue(window.locate_btn.isEnabled())
        window.navigate('naming')
        self.assertEqual(window.preview_tabs.currentIndex(),1)
        window.navigate(second.key)
        self.assertEqual(window.preview_tabs.currentIndex(),0)

    def test_compact_header_reflows_without_losing_paths(self):
        window = self.window
        window.source.setText('C:/sample-in')
        window.destination.setText('D:/sample-out')
        for width,height,wide in ((1280,800,True),(640,600,False),(900,760,False),(1280,800,True)):
            window.resize(width,height)
            _application.processEvents()
            self.assertEqual(window.source.y()==window.destination.y(),wide)
            self.assertEqual(window.source.text(),'C:/sample-in')
            self.assertEqual(window.destination.text(),'D:/sample-out')

    def test_rule_table_grows_with_rules_and_stops_at_eight_rows(self):
        window = self.window
        window.navigate('naming')
        editor = window.naming
        original = editor.table.height()
        editor.add_rule('other','{original}{ext}')
        self.assertGreater(editor.table.height(),original)
        for _ in range(6):
            editor.add_rule('other','{original}{ext}')
        maximum = editor.table.height()
        editor.add_rule('other','{original}{ext}')
        self.assertEqual(editor.table.height(),maximum)
        editor.table.setCurrentCell(0,0)
        editor.remove_rule()
        self.assertEqual(editor.table.rowCount(),9)

    def test_preview_category_filter_is_read_only_and_survives_refresh(self):
        window = self.window
        window.navigate('naming')
        before = window.state.revision
        index = window.category_filter.findData('图片')
        window.category_filter.setCurrentIndex(index)
        visible = lambda:[r for r in range(window.filename_table.rowCount()) if not window.filename_table.isRowHidden(r)]
        self.assertEqual(len(visible()),4)
        self.assertEqual(window.filename_count.text(),'4 / 6 项')
        self.assertEqual(window.state.revision,before)
        window.naming.table.item(0,1).setText('filtered_{n}{ext}')
        window.refresh()
        self.assertEqual(window.category_filter.currentData(),'图片')
        self.assertEqual(len(visible()),4)
        self.assertEqual(window.filename_table.item(0,1).text(),'filtered_1.png')
        window.category_filter.setCurrentIndex(0)
        self.assertEqual(len(visible()),6)

    def test_optional_location_columns_and_error_message_visibility(self):
        window = self.window
        window.navigate('naming')
        self.assertTrue(window.filename_table.isColumnHidden(2))
        self.assertTrue(window.filename_table.isColumnHidden(3))
        self.assertFalse(window.preview_status.isVisible())
        window.location_columns.click()
        self.assertFalse(window.filename_table.isColumnHidden(3))
        self.assertEqual(window.filename_table.item(0,3).text(),'图片/图片_1.zip')
        window.navigate(window.state.layers[0].key)
        window.layer_editor.template.setText('duplicate')
        window.preview_tabs.setCurrentIndex(1)
        window.refresh()
        self.assertTrue(window.preview_status.isVisible())
        self.assertIn('不可用',window.preview_status.text())

    def test_navigation_roles_and_accessibility_are_separate(self):
        from sortzip_core.workbench_controls import NodeRole
        from PySide6.QtCore import Qt
        window = self.window
        for index in (0,1,window.navigation.count()-1):
            item = window.navigation.item(index)
            self.assertIsNone(item.data(NodeRole.LayerNumber))
            self.assertFalse(item.data(NodeRole.Title)[0].isdigit())
        first,outer = window.navigation.item(2),window.navigation.item(3)
        self.assertEqual(first.sizeHint().height(),76)
        self.assertEqual(first.data(NodeRole.Summary),'ZIP · 每包 2 文件')
        self.assertEqual(outer.data(NodeRole.Summary),'ZIP · 每包 2 单元')
        self.assertTrue(outer.data(NodeRole.Outer))
        self.assertIn('最外层',outer.data(Qt.AccessibleTextRole))
        self.assertIn('共 2 组',outer.toolTip())
        window.navigate(window.state.layers[0].key)
        window.layer_editor.password.setText('short')
        window.refresh()
        self.assertEqual(first.data(NodeRole.Title),'第 1 层')
        self.assertEqual(first.data(NodeRole.Error),'密码未确认')
        self.assertIn('密码未确认',first.data(Qt.AccessibleTextRole))

    def test_layer_number_boundaries_and_titles_do_not_depend_on_string_slicing(self):
        from sortzip_core.workbench_prototype import LayerDraft
        from sortzip_core.workbench_controls import NodeRole,DigitTiles
        window = self.window
        window.state.layers = [LayerDraft() for _ in range(100)]
        window.rebuild_navigation()
        for number in (9,10,99,100):
            window.navigate(window.state.layers[number-1].key)
            item = window.navigation.currentItem()
            self.assertEqual(item.data(NodeRole.LayerNumber),number)
            self.assertEqual(window.editor_title.text(),f'第 {number} 层')
            self.assertEqual(DigitTiles.text(number),str(number).zfill(2))
        self.assertTrue(window.navigation.currentItem().data(NodeRole.Outer))
        key = window.state.layers[-2].key
        window.remove_layer()
        self.assertEqual(window.state.selected,key)
        self.assertEqual(window.navigation.currentItem().data(NodeRole.LayerNumber),99)
        self.assertTrue(window.navigation.currentItem().data(NodeRole.Outer))

    def test_digit_cache_and_reserved_regions_fit_at_high_dpi(self):
        from sortzip_core.workbench_controls import DigitTiles,FlowNavigationDelegate
        from PySide6.QtGui import QImage,QPainter,QColor
        from PySide6.QtCore import QRectF,Qt
        tiles = DigitTiles()
        font = self.window.font()
        tile,_,_ = tiles.tile(font,'0',2,QColor('#9caec4'))
        count = len(tiles.tiles)
        self.assertIs(tiles.tile(font,'0',2,QColor('#9caec4'))[0],tile)
        self.assertEqual(len(tiles.tiles),count)
        self.assertEqual(len(next(iter(tiles.paths.values()))[0]),10)
        title,summary,number,badge = FlowNavigationDelegate.regions(QRectF(0,0,176,76),100)
        self.assertGreaterEqual(number.left()-title.right(),8)
        self.assertGreater(number.top(),badge.bottom())
        for value in (1,9,10,99,100):
            image = QImage(352,152,QImage.Format_ARGB32)
            image.setDevicePixelRatio(2)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            tiles.paint(painter,number,value,font,QColor('#9caec4'))
            painter.end()
            pixels = [(x,y) for y in range(image.height()) for x in range(image.width()) if image.pixelColor(x,y).alpha()]
            self.assertTrue(pixels)
            self.assertTrue(all(number.left()*2<=x<number.right()*2 and number.top()*2<=y<number.bottom()*2 for x,y in pixels))

    def test_combo_mouse_focus_remains_neutral_without_clearing_focus(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window = self.window
        window.navigate('naming')
        combo = window.category_filter
        QTest.mouseClick(combo,Qt.LeftButton)
        combo.hidePopup()
        # The offscreen platform leaves the popup active; emulate OS activation.
        _application.processEvents()
        window.activateWindow()
        _application.processEvents()
        self.assertTrue(combo.hasFocus())
        self.assertFalse(combo.property('keyboardFocus'))
        combo.setCurrentIndex(1)
        self.assertFalse(combo.property('keyboardFocus'))
        self.assertEqual(window.filename_count.text(),'4 / 6 项')

    def test_combo_tab_backtab_popup_and_keyboard_signal(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest,QSignalSpy
        window = self.window
        window.navigate('naming')
        combo = window.category_filter
        window.setTabOrder(window.source,combo)
        window.setTabOrder(combo,window.destination)
        window.source.setFocus()
        QTest.keyClick(window.source,Qt.Key_Tab)
        self.assertTrue(combo.hasFocus())
        self.assertTrue(combo.property('keyboardFocus'))
        spy = QSignalSpy(combo.currentIndexChanged)
        QTest.keyClick(combo,Qt.Key_Down)
        self.assertEqual(spy.count(),1)
        self.assertEqual(window.filename_count.text(),'4 / 6 项')
        combo.showPopup()
        QTest.keyClick(combo.view(),Qt.Key_Escape)
        _application.processEvents()
        window.activateWindow()
        _application.processEvents()
        self.assertTrue(combo.hasFocus())
        self.assertTrue(combo.property('keyboardFocus'))
        window.destination.setFocus()
        self.assertFalse(combo.property('keyboardFocus'))
        QTest.keyClick(window.destination,Qt.Key_Backtab)
        self.assertTrue(combo.property('keyboardFocus'))
        combo.setEnabled(False)
        self.assertFalse(combo.property('keyboardFocus'))

    def test_format_combo_factory_does_not_change_production_default(self):
        from sortzip_core.workbench_controls import QuietFocusComboBox
        from sortzip_core.layer_editor import LayerRow
        from sortzip_core.nesting import Layer
        from PySide6.QtWidgets import QComboBox
        row = LayerRow(Layer())
        self.addCleanup(row.deleteLater)
        self.assertIs(type(row.format),QComboBox)
        self.assertIsInstance(self.window.layer_editor.format,QuietFocusComboBox)

    def test_all_numerals_are_centered_in_the_same_maximum_width_cell(self):
        from sortzip_core.workbench_controls import DigitTiles
        from PySide6.QtGui import QFont,QFontDatabase
        font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
        self.assertGreaterEqual(font_id,0,'Windows integration requires its Segoe UI font')
        self.addCleanup(QFontDatabase.removeApplicationFont,font_id)
        font = QFont(QFontDatabase.applicationFontFamilies(font_id)[0],10)
        tiles = DigitTiles()
        _,(paths,width,height) = tiles.glyphs(font)
        centers = [path.boundingRect().center().x() for path in paths]
        self.assertLess(max(centers)-min(centers),.000001)
        for path in paths:
            self.assertGreaterEqual(path.boundingRect().left(),1)
            self.assertLessEqual(path.boundingRect().right(),width-4)
        self.assertLess(paths[1].boundingRect().width(),paths[2].boundingRect().width())

    def test_combo_has_one_uniform_flat_border_with_and_without_keyboard_focus(self):
        from PySide6.QtCore import Qt
        window = self.window
        window.navigate('naming')
        combo = window.category_filter
        for dark in (False,True):
            if window._dark!=dark:
                window.toggle_theme()
            expected = '#3c4a5d' if dark else '#d9e1eb'
            for keyboard in (False,True):
                window.source.setFocus()
                combo.setFocus(Qt.TabFocusReason if keyboard else Qt.MouseFocusReason)
                _application.processEvents()
                pixmap = combo.grab()
                image = pixmap.toImage()
                scale = pixmap.devicePixelRatio()
                # Sample the centers of the physical pixels within the 1px frame.
                inset = int(scale/2)
                samples = [(inset,image.height()//2),(image.width()-1-inset,image.height()//2),
                           (image.width()//2,inset),(image.width()//2,image.height()-1-inset)]
                self.assertEqual([image.pixelColor(x,y).name() for x,y in samples],[expected]*4)
                self.assertEqual(bool(combo.property('keyboardFocus')),keyboard)

    def test_preview_selected_rows_keep_readable_colors_and_text_alignment(self):
        from PySide6.QtGui import QPalette
        window = self.window
        window.navigate('naming')
        table = window.filename_table
        def color_pixels(image,rectangle,color):
            scale = image.devicePixelRatio()
            return {(x,y) for y in range(int(rectangle.top()*scale),int((rectangle.bottom()+1)*scale))
                    for x in range(int(rectangle.left()*scale),int((rectangle.right()+1)*scale))
                    if image.pixelColor(x,y).name()==color}
        for dark in (False,True):
            if window._dark!=dark:
                window.toggle_theme()
            expected_background,expected_text = ('#2a4059','#edf2f7') if dark else ('#e7f1ff','#203044')
            for view in (window.tree,table):
                for group in (QPalette.Active,QPalette.Inactive):
                    self.assertEqual(view.palette().color(group,QPalette.Highlight).name(),expected_background)
                    self.assertEqual(view.palette().color(group,QPalette.HighlightedText).name(),expected_text)
            table.clearSelection()
            rectangle = table.visualRect(table.model().index(0,0))
            unselected = table.viewport().grab().toImage()
            before = color_pixels(unselected,rectangle,expected_text)
            self.assertTrue(before)
            table.selectRow(0)
            table.setFocus()
            _application.processEvents()
            selected = table.viewport().grab().toImage()
            after = color_pixels(selected,rectangle,expected_text)
            self.assertEqual(before,after)
            self.assertGreater(len(color_pixels(selected,rectangle,expected_background)),100)
            window.source.setFocus()
            _application.processEvents()
            inactive = table.viewport().grab().toImage()
            self.assertEqual(after,color_pixels(inactive,rectangle,expected_text))

    def test_preview_scrollbar_is_compact_and_keeps_full_horizontal_range(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window = self.window
        window.resize(640,600)
        window.navigate('naming')
        window.location_columns.setChecked(True)
        _application.processEvents()
        scrollbar = window.filename_table.horizontalScrollBar()
        self.assertTrue(scrollbar.isVisible())
        self.assertEqual(scrollbar.height(),10)
        self.assertGreater(scrollbar.maximum(),0)
        scrollbar.setValue(0)
        scrollbar.setFocus()
        QTest.keyClick(scrollbar,Qt.Key_End)
        self.assertEqual(scrollbar.value(),scrollbar.maximum())
        QTest.keyClick(scrollbar,Qt.Key_Home)
        self.assertEqual(scrollbar.value(),0)
        window.location_columns.setChecked(False)
        _application.processEvents()
        self.assertFalse(scrollbar.isVisible())
        self.assertEqual(scrollbar.value(),0)

    def test_more_information_toolbar_has_padding_and_aligned_controls(self):
        window = self.window
        window.navigate('naming')
        for width,height in ((1280,800),(640,600)):
            window.resize(width,height)
            _application.processEvents()
            self.assertEqual(window.location_columns.text(),'更多信息')
            margins = window.filename_tools.layout().contentsMargins()
            self.assertGreaterEqual(margins.top(),8)
            self.assertGreaterEqual(window.category_filter.y(),8)
            self.assertGreaterEqual(window.filename_tools.height()-window.category_filter.geometry().bottom()-1,8)
            self.assertLessEqual(abs(window.category_filter.geometry().center().y()-window.location_columns.geometry().center().y()),1)

    def test_selected_tree_row_keeps_visible_and_working_expansion_arrow(self):
        from PySide6.QtCore import Qt,QPoint
        from PySide6.QtTest import QTest
        window = self.window
        window.navigate(window.state.layers[0].key)
        tree = window.tree
        item = tree.topLevelItem(0)
        tree.setCurrentItem(item)
        tree.setFocus()
        _application.processEvents()
        rectangle = tree.visualItemRect(item)
        self.assertGreater(rectangle.left(),4)
        image = tree.viewport().grab().toImage()
        scale = image.devicePixelRatio()
        pixels = [image.pixelColor(x,y) for y in range(int((rectangle.top()+4)*scale),int((rectangle.bottom()-3)*scale))
                  for x in range(int(scale),int((rectangle.left()-1)*scale))]
        self.assertTrue(any(color.red()+color.green()+color.blue()<600 for color in pixels))
        self.assertTrue(item.isExpanded())
        QTest.mouseClick(tree.viewport(),Qt.LeftButton,pos=QPoint(rectangle.left()-tree.indentation()//2,rectangle.center().y()))
        self.assertFalse(item.isExpanded())
