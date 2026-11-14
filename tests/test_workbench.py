"""Production workbench contracts with isolated settings, sources and credentials."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from sortzip_core.nesting import Layer
from sortzip_core.workspace_state import WorkspaceState,PreviewResult,draft_from_layer,load_workspace
from sortzip_core.workspace_planning import build_workspace_preview,inspect_resume


class WorkspacePlanningTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'src'
        self.source.mkdir()
        for name in ('a.txt','b.txt','image.png'):
            (self.source/name).write_text(name)
        self.state = WorkspaceState(source=str(self.source),destination=str(self.root/'out'),
            mappings=[('.txt','文档'),('.png','图片')],mapping_enabled=[True,False])

    def preview(self,strict=False):
        return build_workspace_preview(self.state.to_config(strict),self.state.revision,strict=strict)

    def test_readonly_real_scan_and_same_final_paths(self):
        before = list(self.root.rglob('*'))
        result = self.preview(True)
        self.assertEqual(result.total_files,3)
        self.assertEqual(len(result.entries),2)
        self.assertEqual(result.prepared['execution_plan'],result.file_plan)
        self.assertTrue(all(row['archive'].startswith('layer-1/文档/') for row in result.file_plan))
        self.assertEqual(before,list(self.root.rglob('*')))

    def test_structure_survives_unconfirmed_password(self):
        self.state.layers[0].data.update(encrypt=True,password='short',confirm='other')
        result = self.preview()
        self.assertFalse(result.file_error or result.archive_error)
        self.assertEqual(len(result.plan.tasks),2)
        self.state.preview = result
        self.assertIn('密码',self.state.structure()[2][0])
        with self.assertRaisesRegex(ValueError,'密码'):
            self.state.to_config()

    def test_password_text_alone_controls_encryption_and_clearing_disables_it(self):
        data = self.state.layers[0].data
        data.update(encrypt=False,password='password123',confirm='password123')
        self.assertEqual(self.state.layer_values()[0].password,'password123')
        data.update(password='short',confirm='short')
        with self.assertRaisesRegex(ValueError,'8 位'):
            self.state.layer_values()
        data.update(password='password123',confirm='different')
        with self.assertRaisesRegex(ValueError,'不一致'):
            self.state.layer_values()
        data.update(encrypt=True,password='')
        self.assertEqual(self.state.layer_values()[0].password,'')
        data.update(password='password123',confirm='password123',format='tar')
        self.assertEqual(self.state.layer_values()[0].password,'')

    def test_invalid_archive_preserves_filename_preview(self):
        self.state.layers[0].data['template'] = 'same'
        result = self.preview()
        self.assertEqual(len(result.entries),2)
        self.assertTrue(result.archive_error)
        self.assertFalse(result.file_error)

    def test_recursive_hierarchy_rule_inheritance_and_manual_order(self):
        for name in ('sub/c.txt','sub/d.txt'):
            path = self.source/name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
        self.state.recursive = self.state.keep_hierarchy = True
        self.state.rules = [('文档','file_{n}{ext}')]
        self.state.rule_enabled = [True]
        self.state.preview_order = {'文档/sub':['sub/d.txt','sub/c.txt']}
        result = self.preview(True)
        self.assertFalse(result.file_error or result.archive_error)
        rows = [row for row in result.file_plan if row['folder']=='文档/sub']
        self.assertEqual([r['source_rel'] for r in rows],['sub/d.txt','sub/c.txt'])
        self.assertEqual([r['target_name'] for r in rows],['file_1.txt','file_2.txt'])

    def test_output_subtree_excluded_and_cancel_does_not_publish(self):
        self.state.destination = str(self.source/'out')
        Path(self.state.destination).mkdir()
        (Path(self.state.destination)/'generated.txt').write_text('ignore')
        self.state.recursive = True
        result = self.preview()
        self.assertEqual(result.total_files,3)
        self.assertIsNone(build_workspace_preview(self.state.to_config(False),2,lambda:True))

    def test_no_compression_uses_only_enabled_rules(self):
        self.state.compression_enabled = False
        self.state.layers[0].data.update(encrypt=True,password='invalid')
        self.state.rules = [('文档','bad'),('*','{n}{ext}')]
        self.state.rule_enabled = [False,True]
        result = self.preview(True)
        self.assertFalse(result.file_error or result.archive_error)
        self.assertEqual([row['target'] for row in result.entries],['1.txt','2.txt'])
        self.assertNotIn('compression_layers',result.prepared)
        self.assertFalse(result.plan.tasks)

    def test_strict_rejects_occupied_destination_and_empty_task(self):
        (self.root/'out'/'文档').mkdir(parents=True)
        self.assertIn('已存在',self.preview(True).archive_error)
        self.state.destination = str(self.root/'other')
        self.state.mapping_enabled = [False,False]
        self.assertIn('没有符合',self.preview(True).archive_error)

    def test_multiple_layers_do_not_split_previous_units(self):
        self.state.layers = [draft_from_layer(Layer(2,enable_volume=True,volume='1k')),
            draft_from_layer(Layer(2,archive_format='7z',suffix='.png'))]
        result = self.preview(True)
        self.assertFalse(result.archive_error)
        self.assertEqual(len(result.plan.tasks),2)
        self.assertEqual(len(result.plan.tasks[-1].input_ids),1)
        self.assertTrue(result.file_plan[0]['archive'].endswith('.png'))

    def test_bad_resume_rejected_without_writes(self):
        checkpoint = self.root/'.sortzip_resume.json'
        checkpoint.write_text('{"kind":"multilayer","schema":2,"tasks":[]}')
        before = checkpoint.read_bytes()
        self.assertTrue(inspect_resume(dict(resume_from=str(checkpoint)),0).error)
        self.assertEqual(checkpoint.read_bytes(),before)

    def test_cached_metadata_replans_without_scan_or_file_stat(self):
        result = self.preview()
        config = self.state.to_config(False)
        config.update(_preview_cache=result,sort_by='size_desc',naming_rules=[dict(match_folder='*',template='{n}{ext}')])
        original = Path.stat
        def stat(path,*args,**kwargs):
            if path.suffix in ('.txt','.png'): raise AssertionError('cached files must not be restatted')
            return original(path,*args,**kwargs)
        with patch.object(Path,'iterdir',side_effect=AssertionError('unexpected scan')),patch.object(Path,'stat',stat):
            cached = build_workspace_preview(config,1)
        self.assertFalse(cached.file_error or cached.archive_error)
        self.assertEqual(cached.entries[0]['target'],'1.txt')

    def test_destination_filter_is_applied_after_raw_inventory_cache(self):
        out = self.source/'previous-output'
        out.mkdir();(out/'generated.txt').write_text('generated')
        self.state.recursive = True
        self.state.destination = str(out)
        first = self.preview()
        self.assertEqual(first.total_files,3)
        config = self.state.to_config(False)
        config.update(dest=str(self.root/'other'),_preview_cache=first)
        second = build_workspace_preview(config,1)
        self.assertEqual(second.total_files,4)
        (self.source/'new.txt').write_text('new')
        strict = build_workspace_preview(config,2,strict=True)
        self.assertEqual(strict.total_files,5)


@unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in production Qt')
class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSettings
        from sortzip_core.workbench import WorkbenchWindow
        from sortzip_core.smoke import _MemoryCredentials
        global _application
        _application = QApplication.instance() or QApplication([])
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat)
        self.store = _MemoryCredentials()
        self.window = WorkbenchWindow(self.settings,self.store,check_updates=False)
        self.window.show()
        _application.processEvents()
        _application.processEvents()
        self.addCleanup(self.dispose)
        self.errors,self.results = [],[]
        self.error_patch = patch('sortzip_core.workbench.show_styled_dialog',side_effect=lambda parent,title,message,**kw:self.errors.append(message))
        self.stats_patch = patch('sortzip_core.workbench.show_stats_dialog',side_effect=lambda parent,stats,**kw:self.results.append((stats,kw)))
        self.error_patch.start()
        self.stats_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.addCleanup(self.stats_patch.stop)

    def wait(self,predicate,timeout=8):
        from PySide6.QtTest import QTest
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            _application.processEvents()
            # qWait can retain the GIL on this PySide build; let Python planners run.
            time.sleep(.005)
        self.assertTrue(predicate(),'timed out')

    def test_unpack_menu_runs_password_prompt_on_main_thread(self):
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QInputDialog
        w = self.window
        self.assertFalse(w.delivery_manifest.isChecked())
        self.assertFalse(w.delivery_original.isEnabled())
        w.delivery_manifest.setChecked(True)
        self.assertTrue(w.delivery_original.isEnabled())
        config = dict(dest=str(self.root/'unpack-output'),unpack_manifests=['sample.sortzip.json'])
        def run(settings,stats,progress,cancel,force,prompt):
            self.assertNotEqual(QThread.currentThread(),_application.thread())
            self.assertEqual(prompt(2,'sample.data',True),'typed once')
            return 'completed'
        with patch('sortzip_core.unpack_dialog.choose_task',return_value=config),patch(
                'sortzip_core.unpack.run',side_effect=run),patch.object(
                QInputDialog,'getText',return_value=('typed once',True)) as dialog:
            w.commands['unpack'].trigger()
            self.wait(lambda:bool(self.results))
            dialog.assert_called_once()
            self.assertIn('7-Zip',dialog.call_args.args[2])
        self.assertEqual(self.results[0][1]['status'],'completed')

    def dispose(self):
        from shiboken6 import isValid
        if isValid(self.window):
            self.window.close()
            self.wait(lambda:not isValid(self.window) or not self.window.isVisible(),15)
        _application.processEvents()

    def task(self,count=3,compress=False):
        source = self.root/'src'
        source.mkdir(exist_ok=True)
        for index in range(count):
            (source/f'file{index}.txt').write_text('generated-'+str(index))
        self.window.source.setText(str(source))
        self.window.destination.setText(str(self.root/'out'))
        self.window._insert_mapping('.txt','文档')
        self.window.compression_cb.setChecked(compress)
        self.window.keep.setChecked(True)
        self.window.state.confirm_config = False
        self.window.refresh()
        self.wait(lambda:self.window.state.preview is not None and self.window.state.preview.revision==self.window.state.revision)
        return source

    def test_real_default_entry_has_no_demo_or_hidden_legacy_controls(self):
        from sortzip_core.workbench_view import LiveRuleDelegate
        w = self.window
        self.assertEqual(len(w.naming.table.findChildren(LiveRuleDelegate)),1)
        self.assertEqual(len(w.state.layers),1)
        self.assertEqual(w.source.text(),'')
        self.assertFalse(hasattr(w,'stack'))
        self.assertFalse(hasattr(w,'first_cb'))
        self.assertTrue(w.run_btn.isEnabled())
        self.assertEqual(w.windowTitle(),'SortZip')
        self.assertFalse(w.windowIcon().isNull())

    def test_loading_state_and_layer_blocks_edit_notifications(self):
        from PySide6.QtTest import QSignalSpy
        w = self.window
        w._insert_mapping('.txt','文档')
        spies = [QSignalSpy(w.source.textChanged),QSignalSpy(w.mapping_table.itemChanged),
                 QSignalSpy(w.naming.table.itemChanged),QSignalSpy(w.layer_editor.changed)]
        w.state.source = 'loaded-source'
        w.state.layers[0].data.update(group=9,format='7z',encrypt=True,password='session-test',confirm='session-test')
        w.state.selected = w.state.layers[0].key
        w._populate_controls()
        self.assertEqual([spy.count() for spy in spies],[0,0,0,0])
        self.assertEqual(w.layer_editor.group.value(),9)
        self.assertEqual(w.layer_editor.password.text(),'session-test')

    def test_native_caption_tracks_loaded_theme_without_changing_frame_or_identity(self):
        from PySide6.QtCore import Qt
        w = self.window
        flags,icon = w.windowFlags(),w.windowIcon().cacheKey()
        self.assertFalse(flags & Qt.WindowType.FramelessWindowHint)
        w.state.dark_mode = True
        w._populate_controls()
        self.assertEqual(w.native_caption.theme,(True,'#171d25','#edf2f7'))
        w.toggle_theme()
        self.assertEqual(w.native_caption.theme,(False,'#f7f7f8','#202124'))
        self.assertEqual(w.windowFlags(),flags)
        self.assertEqual(w.windowIcon().cacheKey(),icon)
        self.assertEqual(w.windowTitle(),'SortZip')

    def test_fresh_width_enters_side_by_side_and_cards_align(self):
        from PySide6.QtCore import QPoint,Qt
        from sortzip_core.workbench_prototype import WORKBENCH_WIDE_WIDTH
        w = self.window
        available = _application.primaryScreen().availableGeometry()
        self.assertLessEqual(w.width(),min(WORKBENCH_WIDE_WIDTH,w.available_client_rect.width()))
        if available.width()>WORKBENCH_WIDE_WIDTH+16:
            self.assertEqual(w.width(),WORKBENCH_WIDE_WIDTH)
        w.resize(WORKBENCH_WIDE_WIDTH,800)
        _application.processEvents()
        self.assertEqual(w.content.orientation(),Qt.Orientation.Horizontal)
        tops = [widget.mapTo(w,QPoint(0,0)).y() for widget in
                (w.navigation,w.edit_scroll,w.content.widget(1))]
        self.assertLessEqual(max(tops)-min(tops),1)
        self.assertLess(w.navigation_heading.mapTo(w,QPoint()).y(),tops[0])
        w.resize(WORKBENCH_WIDE_WIDTH-1,760)
        _application.processEvents()
        self.assertEqual(w.content.orientation(),Qt.Orientation.Vertical)

    def test_saved_window_width_is_preserved(self):
        from sortzip_core.workbench import WorkbenchWindow
        w = self.window
        w.resize(700,760)
        _application.processEvents()
        w._save_settings()
        restored = WorkbenchWindow(self.settings,self.store,check_updates=False)
        try:
            restored.show()
            _application.processEvents()
            self.assertEqual(restored.width(),min(700,restored.available_client_rect.width()))
            self.assertLessEqual(restored.frameGeometry().right(),restored.screen().availableGeometry().right())
        finally:
            restored.close()
            from shiboken6 import isValid
            self.wait(lambda:not isValid(restored) or not restored.isVisible())

    def test_low_logical_height_scrolls_workspace_and_keeps_footer_reachable(self):
        from PySide6.QtCore import QRect,QPoint
        w = self.window
        w.fit_screen(QRect(0,0,1000,520))
        _application.processEvents()
        self.assertTrue(w.compact_height)
        self.assertLessEqual(w.frameGeometry().bottom(),519)
        self.assertGreater(w.workspace_scroll.verticalScrollBar().maximum(),0)
        self.assertTrue(w.source.isVisible())
        self.assertLessEqual(w.run_btn.mapTo(w,QPoint()).y()+w.run_btn.height(),w.height())
        self.assertEqual(w.run_btn.parentWidget(),w)
        w.log_toggle.setChecked(True)
        _application.processEvents()
        self.assertLessEqual(w.run_btn.mapTo(w,QPoint()).y()+w.run_btn.height(),w.height())
        self.assertLessEqual(w.destination.mapTo(w,QPoint()).y()+w.destination.height(),w.height())
        w.fit_screen(QRect(0,0,1600,1000))
        self.assertFalse(w.compact_height)

    def test_fusion_actions_are_shared_and_layer_commands_trigger_once(self):
        from PySide6.QtGui import QAction
        w = self.window
        self.assertIsInstance(w.copy_btn,QAction)
        self.assertIn(w.copy_btn,w.layer_menu_btn.menu().actions())
        self.assertFalse(w.copy_btn.isEnabled())
        w.commands['add_layer'].trigger()
        self.assertEqual(len(w.state.layers),2)
        w.commands['copy_layer'].trigger()
        self.assertEqual(len(w.state.layers),3)
        w.commands['remove_layer'].trigger()
        self.assertEqual(len(w.state.layers),2)
        w.navigate('mapping')
        self.assertFalse(w.layer_menu_btn.isEnabled())
        before = w.naming.table.rowCount()
        w.commands['rule_add'].trigger()
        self.assertEqual(w.naming.table.rowCount(),before+1)

    def test_fusion_menu_drop_logging_and_removed_buttons(self):
        from PySide6.QtWidgets import QPushButton,QLineEdit
        w = self.window
        self.assertIn(w.review_btn,w.resume_btn.menu().actions())
        self.assertIn(w.commands['resume'],w.resume_btn.menu().actions())
        self.assertEqual(w.resume_btn.text(),'任务')
        self.assertFalse(w.cancel_btn.isVisible())
        self.assertFalse(w.progress_bar.isVisible())
        self.assertNotIn('复制本层',[b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)])
        self.assertNotIn('浏览…',[b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)])
        self.assertIn(w.commands['browse_source'],w.source.actions())
        with patch('sortzip_core.workbench.QFileDialog.getExistingDirectory',return_value=str(self.root)):
            w.commands['browse_source'].trigger()
        self.assertEqual(w.source.text(),str(self.root))
        w.log_toggle.trigger()
        self.assertTrue(w.log.isVisible())
        w.log_toggle.trigger()
        self.assertFalse(w.log.isVisible())

    def test_fusion_actions_lock_and_dynamic_picker_category_survives(self):
        w = self.window
        w.commands['common'].setChecked(True)
        self.assertTrue(w.preset_panel.isVisible())
        w.preset_category.setCurrentText('图片')
        self.assertIn('.png',w._pickers)
        w.preset_category.setCurrentText('文档')
        self.assertIn('.txt',w._pickers)
        with patch.object(w.controller,'busy',return_value=True):
            w._set_task_active(True)
            for key in ('resume','summary','map_add','map_all','settings','rule_add','refresh','browse_source','add_layer'):
                self.assertFalse(w.commands[key].isEnabled(),key)
            self.assertTrue(w.commands['logs'].isEnabled())
            self.assertTrue(w.commands['manual'].isEnabled())
            self.assertTrue(w.cancel_btn.isVisible())
            self.assertTrue(w.progress_bar.isVisible())
        w._set_task_active(False)
        self.assertFalse(w.cancel_btn.isVisible())

    def test_mapping_inventory_pickers_checks_and_delete(self):
        from PySide6.QtCore import Qt
        source = self.task()
        (source/'image.png').write_text('image')
        w = self.window
        w._map_all()
        self.wait(lambda:len(w.state.mappings)==2)
        self.assertEqual(w.state.to_config(False)['custom_names'],{'.txt':'文档','.png':'图片'})
        w.preset_category.setCurrentText('文档')
        self.assertTrue(w._pickers['.txt'].isChecked())
        w._pickers['.txt'].click()
        self.assertNotIn('.txt',w.state.to_config(False)['custom_names'])
        w._pickers['.txt'].click()
        self.assertEqual(w.mapping_table.rowCount(),2)
        w.mapping_table.setCurrentCell(0,0)
        w.remove_mapping()
        self.assertEqual(w.mapping_table.rowCount(),1)
        w._clear_mappings()
        self.assertFalse(w.state.to_config(False)['custom_names'])

    def test_live_enabled_naming_rules_and_preset(self):
        from PySide6.QtCore import Qt
        self.task()
        w = self.window
        self.assertEqual(w.naming.table.item(0,0).checkState(),Qt.CheckState.Unchecked)
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        w.naming.table.item(0,1).setText('renamed_{n}{ext}')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(w.filename_table.item(0,1).text(),'renamed_1.txt')
        w.naming.table.setCurrentCell(0,1)
        w._apply_naming_preset(3)
        self.assertIn('{original}',w.state.rules[0][1])
        w.naming.remove_rule()
        self.assertFalse(w.state.rules)

    def test_drafts_stay_independent_and_passwords_not_saved(self):
        w = self.window
        first = w.state.layers[0]
        w.navigate(first.key)
        row = w.layer_editor

        row.password.setText('session-secret-123')
        row.confirm.setText('unfinished')
        row.advanced_toggle.setChecked(True)
        row.show_password.setChecked(True)
        row.template.setText('first_{n}')
        w.add_layer()
        second = w.draft()
        row.template.setText('outer_{n}')
        w.navigate(first.key)
        self.assertEqual(row.password.text(),'session-secret-123')
        self.assertEqual(row.confirm.text(),'unfinished')
        self.assertTrue(row.show_password.isChecked())
        self.assertTrue(row.advanced_toggle.isChecked())
        w._save_settings()
        self.assertNotIn('session-secret',Path(self.settings.fileName()).read_text(encoding='utf-8'))
        restored = load_workspace(self.settings)
        self.assertFalse(restored.layers[0].data['encrypt'])
        self.assertEqual(restored.layers[0].data['password'],'')
        self.assertEqual(restored.layers[1].data['template'],'outer_{n}')
        self.assertNotEqual(first.key,second.key)

    def test_layer_format_lock_restores_preference_across_switches(self):
        w = self.window
        w.navigate(w.state.layers[0].key)
        row = w.layer_editor
        row.group.setValue(7)
        row.volume.setChecked(True)
        row.password.setText('session-password')
        row.confirm.setText('session-password')

        row.format.setCurrentText('gz')
        self.assertFalse(row.group.isEnabled())
        self.assertFalse(row.volume.isEnabled())
        w.add_layer()
        w.navigate(w.state.layers[0].key)
        self.assertEqual(row.group.value(),1)
        row.format.setCurrentText('zip')
        self.assertEqual(row.group.value(),7)
        self.assertTrue(row.volume.isChecked())
        self.assertTrue(row.password.text())

    def test_task_archive_tool_preserves_conflicting_drafts_and_persists_selection(self):
        self.task(compress=True)
        w = self.window
        first = w.state.layers[0]
        w.navigate(first.key)
        w.layer_editor.format.setCurrentText('lzh')
        w.navigate('final')
        w.archive_tool_combo.setCurrentIndex(w.archive_tool_combo.findData('7zip'))
        self.assertEqual(w.state.archive_tool,'7zip')
        self.assertEqual(first.data['format'],'lzh')
        self.assertEqual(load_workspace(self.settings).archive_tool,'7zip')
        with self.assertRaisesRegex(ValueError,'第 1 层.*LZH'):
            w.state.to_config()
        w.navigate(first.key)
        combo = w.layer_editor.format
        self.assertEqual(combo.currentText(),'lzh')
        self.assertFalse(combo.model().item(combo.currentIndex()).isEnabled())
        combo.setCurrentText('7z')
        self.assertEqual(w.state.to_config()['compression_layers'][0].archive_format,'7z')
        w.archive_tool_combo.setCurrentIndex(w.archive_tool_combo.findData('winrar'))
        w.add_layer()
        self.assertEqual(w.layer_editor.format.currentText(),'rar')

    def test_password_apply_and_suffix_transfer(self):
        w = self.window
        first = w.state.layers[0]
        w.navigate(first.key)
        w.layer_editor.rename.setChecked(True)
        w.layer_editor.suffix.setText('.jpg')
        w.add_layer()
        self.assertTrue(w.draft().data['rename'])
        self.assertFalse(first.data['rename'])
        w.navigate(first.key)
        for field in (w.layer_editor.password,w.layer_editor.confirm):
            field.setText('shared-secret-123')

        w.apply_password()
        self.assertEqual(w.state.layers[1].data['password'],'shared-secret-123')
        w.navigate(w.state.layers[-1].key)
        w.copy_layer()
        self.assertEqual(len(w.state.layers),3)
        w.remove_layer()
        w.remove_layer()
        w.remove_layer()
        self.assertEqual(len(w.state.layers),1)
        self.assertTrue(w.layer_editor.rename.isChecked())

    def test_drag_order_uses_source_identity_and_sort_resets(self):
        self.task()
        w = self.window
        self.assertTrue(w.filename_table.dragEnabled())
        table = w.filename_table
        table.blockSignals(True)
        for column in range(table.columnCount()):
            a,b = table.takeItem(0,column),table.takeItem(2,column)
            table.setItem(0,column,b)
            table.setItem(2,column,a)
        table.blockSignals(False)
        w._preview_reordered()
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(w.state.preview_order['文档'][0],'file2.txt')
        self.assertEqual(table.item(0,0).text(),'file2.txt')
        w.sort_combo.setCurrentIndex(w.sort_combo.findData('name_desc'))
        self.assertEqual(w.state.preview_order,{})

    def test_same_result_and_step_switch_do_not_rebuild_filename_rows(self):
        self.task()
        w = self.window
        original = w.filename_table.item(0,0)
        tree = w.tree.topLevelItem(0)
        with patch.object(w.filename_table,'setItem',side_effect=AssertionError('redundant filename rebuild')):
            w._render_preview()
            self.assertIs(w.tree.topLevelItem(0),tree)
            w.toggle_theme()
            w.refresh()
            w.navigate('naming')
            w.navigate(w.state.layers[0].key)
        self.assertIs(w.filename_table.item(0,0),original)

    def test_new_preview_preserves_source_selection_filter_and_scroll(self):
        from PySide6.QtCore import Qt
        self.task(count=40)
        w = self.window
        w.navigate('naming')
        w.category_filter.setCurrentIndex(1)
        w.filename_table.selectRow(22)
        identity = w.filename_table.item(22,0).data(Qt.ItemDataRole.UserRole)
        w.filename_table.scrollToItem(w.filename_table.item(22,0))
        scroll = w.filename_table.verticalScrollBar().value()
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        w.naming.table.item(0,1).setText('new_{n}{ext}')
        self.wait(lambda:w.state.preview.revision==w.state.revision and
            w.filename_table.item(0,1).text().startswith('new_'))
        self.assertEqual(w.category_filter.currentData(),'文档')
        selected = w.filename_table.selectionModel().selectedRows()
        self.assertEqual(len(selected),1)
        self.assertEqual(w.filename_table.item(selected[0].row(),0).data(Qt.ItemDataRole.UserRole),identity)
        self.assertEqual(w.filename_table.verticalScrollBar().value(),scroll)

    def test_load_exception_restores_signals_and_loading_barrier(self):
        w = self.window
        before = (w._loading,w._capturing)
        with patch.object(w.naming,'fit_table',side_effect=ValueError('test load failure')):
            with self.assertRaisesRegex(ValueError,'test load failure'):
                w._populate_controls()
        self.assertEqual((w._loading,w._capturing),before)
        self.assertFalse(w.source.signalsBlocked())
        self.assertFalse(w.naming.table.signalsBlocked())
        w._populate_controls()

    def test_pending_filename_results_are_marked_and_display_edits_keep_summary_enabled(self):
        from PySide6.QtCore import Qt
        self.task()
        w = self.window
        w.navigate('naming')
        original = w.filename_table.item(0,0)
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        w.naming.table.item(0,1).setText('pending_{n}{ext}')
        self.assertTrue(w.preview_status.isVisible())
        self.assertIn('正在更新',w.preview_status.text())
        self.assertIs(w.filename_table.item(0,0),original)
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertFalse(w.preview_status.isVisible())
        w.navigate(w.state.layers[0].key)
        revision = w.state.revision
        self.assertTrue(w.review_btn.isEnabled())
        with patch.object(w.preview_service,'submit',side_effect=AssertionError('display edit planned')):
            w.layer_editor.advanced_toggle.setChecked(True)
        self.assertEqual(w.state.revision,revision)
        self.assertTrue(w.review_btn.isEnabled())

    def test_stale_preview_cannot_replace_current_revision(self):
        self.task()
        w = self.window
        current = w.state.preview
        w._accept_preview(PreviewResult(w.state.revision-1,file_error='stale'))
        self.assertIs(w.state.preview,current)

    def test_structural_undo_keeps_unrelated_edits_and_edited_generated_rule(self):
        w = self.window
        w._insert_mapping('.txt','文档')
        self.assertEqual(w.structure_history.stack.count(),1)
        w.naming.table.item(0,1).setText('custom_{n}{ext}')
        w.source.setText('unrelated-path')
        w.keep.setChecked(True)
        w.structure_history.stack.undo()
        self.assertEqual(w.mapping_table.rowCount(),0)
        self.assertEqual(w.naming.table.item(0,1).text(),'custom_{n}{ext}')
        self.assertEqual(w.source.text(),'unrelated-path')
        self.assertTrue(w.keep.isChecked())
        w.structure_history.stack.redo()
        self.assertEqual(w.mapping_table.rowCount(),1)
        self.assertEqual(w.naming.table.rowCount(),1)

    def test_row_order_undo_preserves_later_text_edits(self):
        w = self.window
        w._apply_all_extensions(('.txt','.png'))
        for table in (w.mapping_table,w.naming.table):
            original = [table.item(i,0).text() for i in range(2)]
            table.rowsAboutToReorder.emit()
            blocked = table.blockSignals(True)
            rows = [[table.takeItem(i,c) for c in range(2)] for i in range(2)]
            for i,row in enumerate(reversed(rows)):
                for c,item in enumerate(row): table.setItem(i,c,item)
            table.blockSignals(blocked)
            table.rowsReordered.emit()
            table.item(0,1).setText('edited-after-drag')
            w.structure_history.stack.undo()
            self.assertEqual([table.item(i,0).text() for i in range(2)],original)
            self.assertEqual(table.item(1,1).text(),'edited-after-drag')
            w.structure_history.stack.redo()
            self.assertEqual([table.item(i,0).text() for i in range(2)],original[::-1])

    def test_file_order_undo_and_busy_history_lock(self):
        self.task()
        w = self.window
        table = w.filename_table
        blocked = table.blockSignals(True)
        for c in range(table.columnCount()):
            first,last = table.takeItem(0,c),table.takeItem(2,c)
            table.setItem(0,c,last);table.setItem(2,c,first)
        table.blockSignals(blocked)
        w._preview_reordered()
        self.assertEqual(w.state.preview_order['文档'][0],'file2.txt')
        w.structure_history.stack.undo()
        self.assertEqual(w.state.preview_order,{})
        w.structure_history.stack.redo()
        self.assertEqual(w.state.preview_order['文档'][0],'file2.txt')
        with patch.object(w,'_task_busy',return_value=True):
            w._sync_action_states()
            self.assertFalse(w.commands['undo'].isEnabled())
            self.assertFalse(w.commands['redo'].isEnabled())
        self.assertTrue(callable(w.actions))
        from sortzip_core.workbench_prototype import WorkbenchPrototype
        self.assertNotIsInstance(w,WorkbenchPrototype)

    def test_layer_delete_undo_restores_identity_invalid_draft_and_session_password(self):
        w = self.window
        w.add_layer(); key = w.draft().key

        w.layer_editor.password.setText('session-only-secret')
        w.layer_editor.confirm.setText('unfinished')
        w.layer_editor.rename.setChecked(True)
        w.layer_editor.suffix.setText('.png')
        w.remove_layer()
        self.assertEqual(len(w.state.layers),1)
        w.structure_history.stack.undo()
        self.assertEqual(len(w.state.layers),2)
        w.navigate(key)
        self.assertEqual(w.layer_editor.password.text(),'session-only-secret')
        self.assertEqual(w.layer_editor.confirm.text(),'unfinished')
        self.assertTrue(w.layer_editor.rename.isChecked())
        self.assertFalse(w.state.layers[0].data['rename'])
        w._save_settings()
        self.assertNotIn('session-only-secret',Path(self.settings.fileName()).read_text(encoding='utf-8'))

    def test_clear_and_rule_add_undo_are_single_operations(self):
        w = self.window
        w._apply_all_extensions(('.txt','.png'))
        self.assertEqual(w.structure_history.stack.count(),1)
        w._clear_mappings()
        self.assertEqual(w.mapping_table.rowCount(),0)
        w.structure_history.stack.undo()
        self.assertEqual(w.mapping_table.rowCount(),2)
        count = w.naming.table.rowCount()
        w.naming.add_rule('*','{original}{ext}')
        w.structure_history.stack.undo()
        self.assertEqual(w.naming.table.rowCount(),count)
        w.commands['rule_clear'].trigger()
        self.assertEqual(w.naming.table.rowCount(),0)
        w.structure_history.stack.undo()
        self.assertEqual(w.naming.table.rowCount(),count)
        w.structure_history.stack.redo()
        self.assertEqual(w.naming.table.rowCount(),0)
        self.assertEqual(w.structure_history.stack.undoLimit(),100)
        with patch.object(w,'_confirm_resume',return_value=True): w._reset_settings()
        self.assertFalse(w.structure_history.stack.canUndo())

    def test_text_undo_remains_native_and_structure_shortcut_works_elsewhere(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        w = self.window
        w._insert_mapping('.txt','文档')
        w.source.setFocus();QTest.keyClicks(w.source,'typing')
        QTest.keyClick(w.source,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(w.source.text(),'')
        self.assertEqual(w.mapping_table.rowCount(),1)
        w.navigation.setFocus()
        QTest.keyClick(w.navigation,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(w.mapping_table.rowCount(),0)
        QTest.keyClick(w.navigation,Qt.Key.Key_Y,Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(w.mapping_table.rowCount(),1)

    def test_display_only_changes_do_not_submit_jobs_and_plan_changes_reuse_inventory(self):
        self.task(compress=True)
        w = self.window
        w.navigate(w.state.layers[0].key)
        with patch.object(w.preview_service,'submit',wraps=w.preview_service.submit) as submit:
            w.layer_editor.advanced_toggle.setChecked(True)
            w.layer_editor.show_password.setChecked(True)

            w.layer_editor.password.setText('different')
            w.layer_editor.confirm.setText('unfinished')
            _application.processEvents()
            self.assertEqual(submit.call_count,0)
        previous = w.preview_coordinator.cache
        w.layer_editor.group.setValue(2)
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(w.state.preview.inventory,previous.inventory)

    def navigation_errors(self):
        from PySide6.QtCore import Qt
        from sortzip_core.workbench_controls import NodeRole
        return {self.window.navigation.item(i).data(Qt.ItemDataRole.UserRole):self.window.navigation.item(i).data(NodeRole.Error)
                for i in range(self.window.navigation.count()) if self.window.navigation.item(i).data(NodeRole.Error)}

    def test_missing_directory_is_global_without_node_errors(self):
        w = self.window
        self.wait(lambda:w.state.preview is not None)
        self.assertIn('输入文件夹',w.preview_status.text())
        self.assertEqual(self.navigation_errors(),{})
        for index in range(w.navigation.count()):
            self.assertNotIn('输入文件夹',w.navigation.item(index).toolTip())

    def test_bad_mapping_marks_only_mapping_and_clears_after_fix(self):
        self.task()
        w = self.window
        w.mapping_table.item(0,1).setText('CON')
        w.refresh()
        self.assertEqual(set(self.navigation_errors()),{'mapping'})
        w.mapping_table.item(0,1).setText('文档')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(self.navigation_errors(),{})

    def test_naming_conflict_marks_only_original_naming(self):
        from PySide6.QtCore import Qt
        self.task()
        w = self.window
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        w.naming.table.item(0,1).setText('same.txt')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(set(self.navigation_errors()),{'naming'})
        self.assertIn('重复',self.navigation_errors()['naming'])

    def test_bad_archive_template_marks_only_its_layer(self):
        self.task(compress=True)
        w = self.window
        w.add_layer()
        outer = w.state.layers[-1]
        w.layer_editor.template.setText('same')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(set(self.navigation_errors()),{outer.key})
        self.assertIn('重复',self.navigation_errors()[outer.key])
        self.assertEqual(w.filename_table.rowCount(),3)

    def test_only_unconfirmed_layer_has_password_marker_when_enabled(self):
        self.task(compress=True)
        w = self.window
        w.add_layer()
        outer = w.state.layers[-1]
        w.layer_editor.password.setText('short')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(set(self.navigation_errors()),{outer.key})
        w.compression_cb.setChecked(False)
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        self.assertEqual(self.navigation_errors(),{})

    def test_real_noncompression_execution_and_list(self):
        self.task()
        w = self.window
        w.output_list.setChecked(True)
        from PySide6.QtCore import Qt
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        w.run_btn.click()
        self.wait(lambda:bool(self.results))
        self.assertFalse(self.errors)
        self.assertEqual(self.results[0][1]['status'],'completed')
        self.assertTrue((self.root/'out'/'文档'/'List.txt').is_file())
        self.assertTrue((self.root/'out'/'文档'/'1.txt').is_file())
        self.assertTrue((self.root/'src'/'file0.txt').is_file())

    def test_running_readonly_navigation_and_duplicate_launch(self):
        self.task(compress=True)
        w = self.window
        entered,release = Event(),Event()
        snapshots = []
        def execute(config,**kwargs):
            snapshots.append(config)
            entered.set()
            release.wait(6)
            return 'completed'
        self.addCleanup(release.set)
        with patch('sortzip_core.tasks.main_from_config',side_effect=execute):
            w._run()
            self.wait(entered.is_set)
            w._run()
            self.assertFalse(w.source.isEnabled())
            self.assertFalse(w.mapping_page.isEnabled())
            self.assertFalse(w.run_btn.isEnabled())
            w.navigate(w.state.layers[0].key)
            self.assertFalse(w.layer_editor.isEnabled())
            self.assertTrue(w.navigation.isEnabled())
            self.assertEqual(len(snapshots),1)
            self.assertEqual(snapshots[0]['compression_layers'][0].group_size,1)
            release.set()
            self.wait(lambda:bool(self.results))
        self.assertTrue(w.run_btn.isEnabled())
        self.assertIsNone(w.controller.thread)

    def test_confirmation_and_worker_share_frozen_snapshot(self):
        self.task()
        w = self.window
        received = []
        def confirm(result):
            received.append(result.prepared)
            w.state.keep_files = False
            w.state.output_list = True
            return True
        with patch.object(w,'_confirm_config',side_effect=confirm):
            w._run()
            self.wait(lambda:bool(self.results))
        self.assertTrue(received[0]['keep_files'])
        self.assertFalse(received[0]['output_list'])
        self.assertTrue((self.root/'src'/'file0.txt').exists())
        self.assertFalse((self.root/'out'/'文档'/'List.txt').exists())

    def test_factory_failure_recovers_and_close_waits(self):
        self.task()
        w = self.window
        with patch.object(w.controller,'factory',side_effect=RuntimeError('failure')):
            w._run()
            self.wait(lambda:bool(self.errors))
        self.assertFalse(w.controller.busy())
        self.assertTrue(w.run_btn.isEnabled())
        entered,release = Event(),Event()
        def execute(config,**kwargs):
            entered.set()
            release.wait(6)
            return 'safe_cancelled'
        self.addCleanup(release.set)
        with patch('sortzip_core.tasks.main_from_config',side_effect=execute):
            w._run()
            self.wait(entered.is_set)
            w.close()
            self.assertTrue(w.isVisible())
            self.assertTrue(w.controller.worker._cancelled)
            release.set()
            from shiboken6 import isValid
            self.wait(lambda:not isValid(w) or not w.isVisible())
        self.assertIsNone(w.controller.thread)

    def test_preflight_rechecks_new_files_and_rejects_bad_password(self):
        source = self.task(count=1)
        (source/'new.txt').write_text('new')
        self.window._run()
        self.wait(lambda:bool(self.results))
        self.assertEqual(self.results[0][0]['files_moved'],2)
        self.window.compression_cb.setChecked(True)
        self.window.navigate(self.window.state.layers[0].key)
        self.window.layer_editor.password.setText('short')
        self.window._run()
        self.assertTrue(any('密码' in message for message in self.errors))

    def test_cancel_preflight_does_not_create_output(self):
        self.task()
        self.window._run()
        self.window.controller.cancel()
        self.wait(lambda:not self.window.controller.busy())
        self.assertFalse((self.root/'out').exists())

    def test_log_masks_all_layer_passwords_and_error_not_100_percent(self):
        w = self.window
        w.controller._secrets = ['first-password','outer-password']
        w.controller._log('first-password outer-password')
        self.assertNotIn('password',w.log.toPlainText())
        w.progress_bar.setValue(42)
        self.assertEqual(w.progress_bar.value(),42)

    def test_old_preferences_migrate_without_changing_engine_layer_identity(self):
        from PySide6.QtCore import QSettings
        settings = QSettings(str(self.root/'old.ini'),QSettings.Format.IniFormat)
        settings.setValue('archive_format','7z')
        settings.setValue('group_size',3)
        settings.setValue('double_compress',True)
        state = load_workspace(settings)
        self.assertEqual([layer.group_size for layer in state.layer_values()],[3,1])
        self.assertEqual([layer.archive_format for layer in state.layer_values()],['7z','7z'])

    def test_reset_and_dark_preference(self):
        self.task()
        w = self.window
        w.toggle_theme()
        w._save_settings()
        self.assertTrue(load_workspace(self.settings).dark_mode)
        with patch.object(w,'_confirm_resume',return_value=True):
            w._reset_settings()
        self.assertEqual(w.source.text(),'')
        self.assertEqual(w.mapping_table.rowCount(),0)
        self.assertFalse(w.state.dark_mode)
        self.assertEqual(len(w.state.layers),1)

    def test_dark_owned_dialog_has_readable_background(self):
        from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel
        from PySide6.QtCore import Qt
        w = self.window
        w.toggle_theme()
        dialog = QDialog(w)
        QVBoxLayout(dialog).addWidget(QLabel('取消及统计提示',dialog))
        dialog.resize(320,140)
        dialog.show();_application.processEvents()
        try:
            color = dialog.grab().toImage().pixelColor(4,4)
            self.assertLess(color.lightness(),100)
            self.assertGreater(dialog.palette().color(dialog.foregroundRole()).lightness(),180)
        finally: dialog.close()

    def test_source_drop_and_resume_drop_have_real_handlers(self):
        from PySide6.QtCore import QMimeData,QUrl,QPoint,Qt
        from PySide6.QtGui import QDropEvent
        source = self.root/'dropped'
        source.mkdir()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        event = QDropEvent(QPoint(5,5),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        self.window.source.dropEvent(event)
        self.assertEqual(Path(self.window.state.source),source)
        with patch.object(self.window.controller,'resume') as resume:
            self.window.resume_btn.resumeDropped.emit(str(self.root/'.sortzip_resume.json'))
        resume.assert_called_once()

    def test_keyboard_checkbox_focus_does_not_fill_label(self):
        from PySide6.QtCore import Qt
        w = self.window
        check = w.recursive_cb
        w.activateWindow()
        check.setFocus(Qt.FocusReason.TabFocusReason)
        _application.processEvents()
        self.assertTrue(check.hasFocus())
        image = check.grab().toImage()
        center = image.pixelColor(image.width()//2,image.height()//2)
        self.assertNotEqual(center,w.palette().highlight().color())

    def test_background_pool_discards_canceled_and_stale_jobs(self):
        from sortzip_core.workspace_services import BackgroundPlanner
        entered,release = Event(),Event()
        planner = BackgroundPlanner(self.window)
        received = []
        built = []
        planner.ready.connect(received.append)
        def builder(config,revision,cancelled,strict):
            built.append(revision)
            if revision==1:
                entered.set()
                release.wait(4)
            return PreviewResult(revision)
        self.addCleanup(release.set)
        planner.submit({},1,builder=builder)
        self.wait(entered.is_set)
        planner.submit({},2,builder=builder)
        planner.submit({},3,builder=builder)
        release.set()
        self.wait(lambda:not planner.busy())
        self.assertEqual([result.revision for result in received if result is not None],[3])
        self.assertEqual(built,[1,3])

    def test_repeated_real_tasks_release_native_threads_before_next_launch(self):
        self.task(count=1)
        for index in range(12):
            self.window.destination.setText(str(self.root/f'out-{index}'))
            self.window._run()
            self.wait(lambda:len(self.results)==index+1)
            self.assertIsNone(self.window.controller.thread)
            self.assertIsNone(self.window.controller.worker)
            self.assertEqual(self.results[-1][1]['status'],'completed')

    def test_mapping_hierarchy_preview_matches_classified_path(self):
        source = self.task(count=1)
        (source/'sub').mkdir()
        (source/'sub'/'nested.txt').write_text('nested')
        w = self.window
        w.recursive_cb.setChecked(True)
        w.keep_hierarchy_cb.setChecked(True)
        w.navigate('mapping')
        w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision)
        rows = [w.tree.topLevelItem(index).text(1) for index in range(w.tree.topLevelItemCount())]
        self.assertIn('文档/sub/nested.txt',rows)
        self.assertNotIn('文档/sub/sub/nested.txt',rows)

    def test_migration_failure_remains_blocked_after_save_restart(self):
        w = self.window
        w.state.invalid_migration = True
        w._save_settings()
        restored = load_workspace(self.settings)
        self.assertTrue(restored.invalid_migration)
        with self.assertRaisesRegex(ValueError,'迁移'):
            restored.to_config(True)

    def make_legacy_checkpoint(self,missing=False):
        from sortzip_core import engine
        folder = self.root/'legacy'/'doc'
        folder.mkdir(parents=True)
        if not missing:
            (folder/'a.txt').write_text('a')
        engine._write_checkpoint(folder.parent,dict(config=dict(group_size=1,password=False,enable_volume=False),
            groups=[dict(folder='doc',base_name='1',files=['a.txt'],sizes={'a.txt':1},state='pending')]))
        return folder.parent/'.sortzip_resume.json'

    def test_legacy_resume_keeps_checkpoint_config_and_cancellation(self):
        from PySide6.QtWidgets import QDialog
        from sortzip_core.widgets import PasswordDialog
        checkpoint = self.make_legacy_checkpoint()
        w = self.window
        result = inspect_resume(dict(resume_from=str(checkpoint)),0)
        self.assertFalse(result.error)
        with patch.object(w,'_confirm_resume',return_value=True),patch.object(PasswordDialog,'exec',return_value=QDialog.DialogCode.Accepted):
            config = w._resume_parameters(result)
        self.assertEqual(config['resume_from'],str(checkpoint))
        self.assertNotIn('compression_layers',config)
        with patch.object(w,'_confirm_resume',return_value=False):
            self.assertIsNone(w._resume_parameters(result))
        self.assertEqual(result.groups[0]['state'],'pending')

    def test_invalid_resume_never_prompts_password_or_starts_worker(self):
        checkpoint = self.root/'.sortzip_resume.json'
        checkpoint.write_text('{bad json')
        with patch('sortzip_core.workbench.PasswordDialog') as password,patch.object(self.window.controller,'factory') as factory:
            self.window._start_resume(str(checkpoint))
            self.wait(lambda:not self.window.controller.busy())
        password.assert_not_called()
        factory.assert_not_called()
        self.assertTrue(self.errors)

    def test_failed_task_preserves_progress_and_releases_thread_before_report(self):
        self.task()
        w = self.window
        def fail(config,**kwargs):
            kwargs['on_progress'](0,100,42,100,'处理中')
            raise OSError('generated failure')
        with patch('sortzip_core.tasks.main_from_config',side_effect=fail):
            w._run()
            self.wait(lambda:bool(self.results))
        self.assertEqual(self.results[0][1]['status'],'error')
        self.assertEqual(w.progress_bar.value(),42)
        self.assertIn('失败',w.progress_bar.format())
        self.assertIsNone(w.controller.thread)

    def test_many_files_preview_and_rapid_revisions_use_final_rule(self):
        source = self.task(count=1000)
        w = self.window
        from PySide6.QtCore import Qt
        w.naming.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        for index in range(20):
            w.naming.table.item(0,1).setText(f'edit{index}_{{n}}{{ext}}')
            w.refresh()
        self.wait(lambda:w.state.preview.revision==w.state.revision,20)
        self.assertEqual(len(w.state.preview.entries),1000)
        self.assertEqual(w.filename_table.item(999,1).text(),'edit19_1000.txt')
        self.assertEqual(len(list(source.iterdir())),1000)
        self.assertFalse((self.root/'out').exists())

    def test_real_bandizip_multilayer_disguise_and_resume(self):
        import shutil
        if not shutil.which('bz'):
            self.skipTest('Bandizip not installed')
        self.task(count=3,compress=True)
        w = self.window
        w.navigate(w.state.layers[0].key)
        w.layer_editor.group.setValue(2)
        w.layer_editor.verify.setChecked(True)
        w.add_layer()
        w.layer_editor.group.setValue(2)
        w.layer_editor.format.setCurrentText('zip')
        w.layer_editor.rename.setChecked(True)
        w.layer_editor.suffix.setText('.png')
        w.output_list.setChecked(True)
        self.assertIn('List.txt',w.output_list.toolTip())
        w._run()
        self.wait(lambda:bool(self.results),30)
        self.assertEqual(self.results[0][1]['status'],'completed',self.errors)
        self.assertEqual(self.results[0][0]['file_lists'],1)
        checkpoint = self.root/'out'/'.sortzip_resume.json'
        result = inspect_resume(dict(resume_from=str(checkpoint)),0)
        self.assertFalse(result.error)
        self.assertEqual(result.completed,3)
        import zipfile
        archives = list((self.root/'out'/'layer-2').rglob('*.png'))
        self.assertEqual(len(archives),1)
        with zipfile.ZipFile(archives[0]) as outer:
            self.assertEqual(len(outer.namelist()),2)
        self.assertTrue(list((self.root/'out'/'layer-2').rglob('Restore-Archive-Names')))
        saved = w.state
        w.toggle_theme()
        history_count = w.structure_history.stack.count()
        row_ids = list(saved.mapping_ids)
        with patch.object(w,'_confirm_resume',return_value=True):
            original_show = w._show_resume_plan
            def show_resume(result,passwords):
                original_show(result,passwords)
                self.assertTrue(w._dark)
                self.assertTrue(w.state.dark_mode)
            with patch.object(w,'_show_resume_plan',side_effect=show_resume):
                w._start_resume(str(checkpoint))
                self.wait(lambda:len(self.results)==2,30)
        self.assertIs(w.state,saved)
        self.assertEqual(w.structure_history.stack.count(),history_count)
        self.assertEqual(w.state.mapping_ids,row_ids)
        w.structure_history.stack.undo()
        self.assertEqual(len(w.state.layers),1)
        self.assertEqual(self.results[-1][1]['status'],'completed')
        self.assertEqual(self.results[-1][0]['file_lists'],1)
