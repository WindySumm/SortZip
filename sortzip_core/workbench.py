"""Production flow workbench: shared editors, real plans and task controller."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt,QTimer,QSettings,QUrl,Slot
from PySide6.QtGui import QIcon,QDesktopServices,QTextCursor
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPushButton,QFileDialog,QDialog,QDialogButtonBox,QTableWidgetItem,
    QMessageBox,QGridLayout,QSizePolicy,QLineEdit)

from sortzip_core import __version__
from sortzip_core.archive_tools import TOOL_FORMATS, TOOL_NAMES, check_tool, detect_tool
from sortzip_core.window_fit import WindowFitMixin
from sortzip_core.native_caption import NativeCaption
from sortzip_core.preview_coordinator import PreviewCoordinator
from sortzip_core.workbench_polish import WorkbenchPolishMixin
from sortzip_core.workbench_view import WorkbenchView,LiveRuleDelegate,WORKBENCH_WIDE_WIDTH
from sortzip_core.workbench_controls import QuietFocusComboBox,NodeRole
from sortzip_core.workspace_state import WorkspaceState,PreviewResult,load_workspace,draft_from_layer,SORT_LABELS
from sortzip_core.workspace_services import BackgroundPlanner
from sortzip_core.workspace_tasks import TaskController
from sortzip_core.structure_history import StructureHistory,structural,ROW_ID
from sortzip_core.nesting import Layer
from sortzip_core.constants import EXT_CATEGORIES,RENAME_PRESETS
from sortzip_core.credentials import WindowsCredentialStore,load_password,save_password
from sortzip_core.ui_controls import StateCheckBox,loading_controls
from sortzip_core.widgets import (PasswordDialog,
    resource_path,show_styled_dialog,show_stats_dialog)
from sortzip_core.i18n import LANGUAGE_KEY, LANGUAGE_CHOICES, language_preference, tr


class WorkbenchWindow(WindowFitMixin,WorkbenchPolishMixin,WorkbenchView):
    def __init__(self,settings=None,credential_store=None,check_updates=True,task_factory=None):
        self._initializing = True
        self._capturing = False
        self._closing = False
        self._cancel_dlg = None
        self._resume_saved_state = None
        self._resume_inspection = None
        self._warnings = []
        self.preview_coordinator = PreviewCoordinator(self)
        self.settings = settings if settings is not None else QSettings('SortZip','SortZip')
        self.credential_store = credential_store if credential_store is not None else WindowsCredentialStore()
        self._saved_password = load_password(self.settings,self.credential_store,self._warnings.append)
        super().__init__(load_workspace(self.settings,self._saved_password),production=True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose,True)
        self.timer.stop()
        self._loading = True
        self.setWindowTitle('SortZip')
        self.setWindowIcon(QIcon(resource_path('assets/sortzip.ico')))
        self.native_caption = NativeCaption(self,self._append_log)
        self.layout().setContentsMargins(16,0,16,12)
        self.notice.setText(tr('分类 → 原文件命名 → 逐层压缩。预览只读，确认执行后才处理真实文件。'))
        self.source_label.setText(tr('输入目录'))
        self.destination_label.setText(tr('输出目录'))
        self._build_paths()
        options = QHBoxLayout()
        self.recursive_cb = StateCheckBox(tr('包含子文件夹'))
        self.compression_cb = StateCheckBox(tr('启用压缩'))
        self.refresh_btn = self._tool(self._command('refresh',tr('刷新预览'),self._refresh_requested,'refresh'))
        options.addWidget(self.recursive_cb)
        options.addWidget(self.compression_cb)
        options.addStretch()
        options.addWidget(self.refresh_btn)
        self.layout().insertLayout(2,options)
        self._build_mapping_tools()
        self._build_naming_tools()
        self._configure_filename_preview()
        self.archive_tool_combo = QuietFocusComboBox()
        self.archive_tool_combo.setAccessibleName(tr('压缩工具'))
        for identifier,name in TOOL_NAMES.items():
            self.archive_tool_combo.addItem(name,identifier)
        tool_field = QFormLayout()
        tool_field.addRow(tr('压缩工具'),self.archive_tool_combo)
        self.final_page.layout().insertLayout(0,tool_field)
        self.archive_tool_combo.currentIndexChanged.connect(self._archive_tool_changed)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit_scroll.setMinimumHeight(130)
        self.preview_tabs.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        self.controller = TaskController(self,factory=task_factory)
        self.controller.active.connect(self._set_task_active)
        self.controller.log.connect(self._append_log)
        self.controller.progress.connect(self._update_progress)
        self.controller.failed.connect(self._task_error)
        self.controller.finished.connect(self._task_finished)
        self.controller.idle.connect(self._task_idle)
        self.controller.cancel_waiting.connect(self._on_cancel_waiting)
        self.controller.prepared.connect(self._accept_preview)
        self.preview_service = BackgroundPlanner(self)
        self.preview_service.ready.connect(self._accept_preview)
        self.recursive_cb.toggled.connect(self.change)
        self.compression_cb.toggled.connect(self.change)
        self._polish_workbench()
        self._loading = False
        self._initializing = False
        self._populate_controls()
        self.run_btn.setEnabled(True)
        geometry = self.settings.value('workbench_geometry',self.settings.value('window_geometry'))
        if geometry:
            self.restoreGeometry(geometry)
        else:
            area = QApplication.primaryScreen().availableGeometry()
            self.resize(min(WORKBENCH_WIDE_WIDTH,area.width()),min(800,area.height()))
        from sortzip_core.app import UpdateChecker
        self.install_screen_fit()
        self.structure_history = StructureHistory(self)
        menu = self.resume_btn.menu()
        menu.addSeparator()
        self.commands['undo'] = self.structure_history.stack.createUndoAction(self,tr('撤销'))
        self.commands['redo'] = self.structure_history.stack.createRedoAction(self,tr('重做'))
        menu.addActions([self.commands['undo'],self.commands['redo']])
        self._sync_action_states()
        self._update_checker = UpdateChecker()
        self._update_checker.check_done.connect(self._on_update_result)
        if check_updates:
            self._update_checker.start()
        for message in self._warnings+self.state.notices:
            self._append_log(message+'\n')
        if self._warnings or self.state.notices:
            self.log_toggle.setChecked(True)
        self.refresh()

    def _build_paths(self):
        for name in ('source','destination'):
            field = getattr(self,name)
            self.paths.removeWidget(field)
            field.setPlaceholderText(tr('选择或拖入文件夹'))
            field.setAccessibleName(tr('输入目录') if name=='source' else tr('输出目录'))
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0,0,0,0)
            layout.setSpacing(6)
            layout.addWidget(field,1)
            setattr(self,name+'_container',container)
        self.source_label.setBuddy(self.source)
        self.destination_label.setBuddy(self.destination)
        self._paths_wide = None
        self.layout_paths()

    def layout_paths(self):
        if not hasattr(self,'destination_container'):
            return super().layout_paths()
        wide = self.width()>=WORKBENCH_WIDE_WIDTH
        if wide==self._paths_wide:
            return
        self._paths_wide = wide
        while self.paths.count():
            self.paths.takeAt(0)
        self.paths.setColumnStretch(1,1)
        self.paths.setColumnStretch(3,1 if wide else 0)
        for widget,row,column in ((self.source_label,0,0),(self.source_container,0,1),
                (self.destination_label,0 if wide else 1,2 if wide else 0),
                (self.destination_container,0 if wide else 1,3 if wide else 1)):
            self.paths.addWidget(widget,row,column)

    def _build_mapping_tools(self):
        self.mapping_table.setHorizontalHeaderLabels([tr('文件类型（勾选启用）'),tr('分类目录')])
        self.mapping_table.setItemDelegate(LiveRuleDelegate(self.mapping_table))
        self.mapping_table.rowsReordered.connect(self.change)
        self.preset_category = QuietFocusComboBox()
        for name in EXT_CATEGORIES:
            self.preset_category.addItem(tr(name),name)
        self.preset_panel = QWidget()
        self.preset_grid = QGridLayout(self.preset_panel)
        self.preset_grid.setContentsMargins(0,4,0,4)
        self.mapping_page.layout().insertWidget(1,self.preset_panel)
        self.preset_panel.hide()
        self.preset_category.currentIndexChanged.connect(self._build_pickers)
        self._pickers = {}
        self._map_all_pending = False

    def _build_naming_tools(self):
        self.naming.table.setHorizontalHeaderLabels([tr('匹配目录（勾选启用）'),tr('文件名称模板')])
        self.naming.table.rowsReordered.connect(self.change)
        self.sort_combo = QuietFocusComboBox()
        for label,code in SORT_LABELS.items():
            self.sort_combo.addItem(tr(label),code)
        self.keep_hierarchy_cb = StateCheckBox(tr('保持原文件夹层级'))
        settings = QFormLayout()
        settings.addRow(tr('文件排序'),self.sort_combo)
        settings.addRow(self.keep_hierarchy_cb)
        self.naming.layout().insertLayout(0,settings)
        self.naming_preset = QuietFocusComboBox()
        self.naming_preset.addItem(tr('选择命名预设…'),None)
        for template,label in RENAME_PRESETS:
            self.naming_preset.addItem(tr(label)+'  '+template,template)
        self.naming.layout().insertWidget(2,self.naming_preset)
        self.naming_preset.activated.connect(self._apply_naming_preset)
        self.keep_hierarchy_cb.toggled.connect(self.change)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)

    def _configure_filename_preview(self):
        table = self.filename_table
        table.rowsReordered.connect(self._preview_reordered)

    @staticmethod
    def _checked_item(text,checked):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    def _populate_controls(self):
        with loading_controls(self,self.source,self.destination,self.recursive_cb,
                self.compression_cb,self.keep_hierarchy_cb,self.keep,self.output_list,self.sort_combo,
                self.mapping_table,self.naming.table,self.naming,self.archive_tool_combo):
            self.source.setText(self.state.source)
            self.destination.setText(self.state.destination)
            for widget,value in ((self.recursive_cb,self.state.recursive),(self.compression_cb,self.state.compression_enabled),
                    (self.keep_hierarchy_cb,self.state.keep_hierarchy),(self.keep,self.state.keep_files),(self.output_list,self.state.output_list)):
                widget.setChecked(value)
            self.sort_combo.setCurrentIndex(max(0,self.sort_combo.findData(self.state.sort_by)))
            self.archive_tool_combo.setCurrentIndex(self.archive_tool_combo.findData(self.state.archive_tool))
            for table,rows,flags in ((self.mapping_table,self.state.mappings,self.state.mapping_enabled),
                                     (self.naming.table,self.state.rules,self.state.rule_enabled)):
                table.setRowCount(0)
                for index,(left,right) in enumerate(rows):
                    table.insertRow(index)
                    enabled = flags[index] if index<len(flags) else True
                    item = self._checked_item(left,enabled)
                    ids = self.state.mapping_ids if table is self.mapping_table else self.state.rule_ids
                    if index<len(ids): item.setData(ROW_ID,ids[index])
                    table.setItem(index,0,item)
                    table.setItem(index,1,QTableWidgetItem(right))
            self.naming.fit_table()
            self._dark = self.state.dark_mode
            self.theme_btn.setText(tr('浅色模式') if self._dark else tr('深色模式'))
            self.apply_theme()
            self.rebuild_navigation()
        self._build_pickers()
        self._sync_compression_ui()
        self.preview_coordinator.loaded()

    def change(self,*args):
        if self._initializing or self._loading or self._capturing or self._task_busy():
            return
        self._capturing = True
        previous_revision = self.state.revision
        previous_status = self.preview_status.text()
        try:
            old_mappings = list(self.state.mappings)
            for table in (self.mapping_table,self.naming.table):
                for row in range(table.rowCount()):
                    item = table.item(row,0)
                    if item is not None and not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                        item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Checked)
            super().change(*args)
            self.state.mapping_enabled = [bool(self.mapping_table.item(r,0) and self.mapping_table.item(r,0).checkState()==Qt.CheckState.Checked) for r in range(self.mapping_table.rowCount())]
            self.state.rule_enabled = [bool(self.naming.table.item(r,0) and self.naming.table.item(r,0).checkState()==Qt.CheckState.Checked) for r in range(self.naming.table.rowCount())]
            self.state.recursive = self.recursive_cb.isChecked()
            self.state.keep_hierarchy = self.keep_hierarchy_cb.isChecked()
            self.state.keep_files,self.state.output_list = self.keep.isChecked(),self.output_list.isChecked()
            self.state.compression_enabled = self.compression_cb.isChecked()
            self.state.sort_by = self.sort_combo.currentData()
            if old_mappings!=self.state.mappings:
                self._ensure_category_rules()
            if self.draft() is not None:
                self.state.invalid_migration = False
            self.preview_status.setText(tr('配置已变化，正在更新预览…'))
        finally:
            self._capturing = False
        self._sync_pickers()
        self._sync_compression_ui()
        self.preview_coordinator.changed(previous_revision,previous_status)

    def _ensure_category_rules(self):
        existing = {folder for folder,_ in self.state.rules}
        for folder in dict.fromkeys(folder.strip() for _,folder in self.state.mappings):
            if folder and folder not in existing:
                row = self.naming.table.rowCount()
                self.naming.table.insertRow(row)
                self.naming.table.setItem(row,0,self._checked_item(folder,False))
                self.naming.table.setItem(row,1,QTableWidgetItem('{n}{ext}'))
                self.state.rules.append((folder,'{n}{ext}'))
                self.state.rule_enabled.append(False)
                existing.add(folder)
        self.naming.fit_table()

    def _task_busy(self):
        return hasattr(self,'controller') and self.controller.busy()

    def _sync_compression_ui(self):
        enabled = self.state.compression_enabled and not self._task_busy()
        self.archive_tool_combo.setEnabled(enabled)
        self.layer_editor.setEnabled(enabled)
        self.add_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled and self.draft() is not None)
        self.remove_btn.setEnabled(enabled and self.draft() is not None and len(self.state.layers)>1)
        self._sync_action_states()

    def _archive_tool_changed(self,*args):
        if self._loading or self._task_busy():
            return
        self.save_layer()
        self.state.archive_tool = self.archive_tool_combo.currentData()
        draft = self.draft()
        if draft is not None:
            self.load_layer(draft)
        self.change()
        self._save_settings()

    def select_node(self,item,*args):
        super().select_node(item,*args)
        if not self._initializing:
            self._sync_compression_ui()

    @structural(tr('添加压缩层'))
    def add_layer(self):
        if self._task_busy() or not self.state.compression_enabled:
            return
        self.save_layer()
        fmt = next(iter(TOOL_FORMATS[self.state.archive_tool]))
        self._append_draft(draft_from_layer(Layer(archive_format=fmt)))

    @structural(tr('复制压缩层'))
    def copy_layer(self):
        if not self._task_busy() and self.state.compression_enabled:
            super().copy_layer()

    @structural(tr('删除压缩层'))
    def remove_layer(self):
        if not self._task_busy() and self.state.compression_enabled:
            super().remove_layer()

    def apply_password(self):
        if not self._task_busy():
            super().apply_password()

    def refresh(self):
        if self._initializing:
            return
        self.preview_coordinator.refresh()

    @Slot(object)
    def _accept_preview(self,result):
        if not self.preview_coordinator.accept(result):
            return
        if self._map_all_pending:
            self._map_all_pending = False
            if result.file_error:
                show_styled_dialog(self,tr('无法扫描类型'),result.file_error,width=420,height=200)
            else:
                self._apply_all_extensions(result.extensions)

    def navigation_issues(self):
        result = self.state.preview
        if result is None or result.revision!=self.state.revision:
            return {}
        owners = {f'layer:{index}':draft.key for index,draft in enumerate(self.state.layers)}
        return {owners.get(node,node):message for node,message in result.node_errors.items()}

    def _render_preview(self):
        super().refresh()
        self.locate_btn.setVisible(self.locate_btn.isEnabled())
        self.run_btn.setEnabled(not self._task_busy() and not self._closing)
        result = self.state.preview
        if result is None:
            return
        self.notice.setText(tr('已扫描 {0} 个文件，匹配 {1} 个。', result.total_files, len(result.entries))+
            (tr('存在未匹配类型，可使用“映射全部”。') if result.total_files>len(result.entries) else tr('预览只读，确认执行后才处理文件。')))
        self.summary.setText(tr('输出目录：{0}\n\n匹配 {1} 个文件。', self.state.destination or tr('尚未选择'), len(result.entries))+(tr('共 {0} 层，最外层 {1} 个完整单元。', len(self.state.layers), len(result.plan.final_ids)) if self.state.compression_enabled else tr('未启用压缩，只执行分类、命名与可选清单。'))+
            tr('\n\n保留原始文件时分类仅复制；否则会移动原文件。当前多层流程仍保留中间产物和完成断点。'))
        if not self.state.compression_enabled:
            self.preview_status.setText(tr('未启用压缩：只执行分类、命名和可选清单。')+(' '+result.file_error if result.file_error else ''))
            for index in range(self.navigation.count()):
                item = self.navigation.item(index)
                if item.data(NodeRole.LayerNumber) is not None:
                    item.setData(NodeRole.Summary,tr('未启用压缩 · 配置已保留'))
        if not self._task_busy():
            self._sync_compression_ui()
        self._sync_action_states()

    def filename_rows(self,entries,plan=None):
        if self._initializing or self.state.preview is None:
            return super().filename_rows(entries,plan)
        by_source = {row['source_rel']:row for row in self.state.preview.file_plan}
        if not by_source and self.state.compression_enabled:
            return super().filename_rows(entries,plan)
        return ((entry['source'],entry['target'],entry['folder'],
            (tr('归档计划待修正') if self.state.preview.archive_error else
                by_source.get(entry['source'],{}).get('archive') or tr('归档计划待修正'))
            if self.state.compression_enabled else tr('不压缩')) for entry in entries)

    def filter_filenames(self,*args):
        super().filter_filenames(*args)
        if not self._initializing:
            self._update_drag_enabled()

    def _update_drag_enabled(self):
        folders = {self.filename_table.item(row,2).text() for row in range(self.filename_table.rowCount()) if not self.filename_table.isRowHidden(row)}
        enabled = len(folders)==1 and not self._task_busy() and self._resume_saved_state is None
        self.filename_table.setDragEnabled(enabled)
        self.filename_table.setAcceptDrops(enabled)

    @structural(tr('调整原文件顺序'))
    def _preview_reordered(self):
        if self._task_busy():
            return
        self._filename_render_key = None
        folders = {}
        for row in range(self.filename_table.rowCount()):
            item = self.filename_table.item(row,0)
            folder = item.data(Qt.ItemDataRole.UserRole+1)
            folders.setdefault(folder,[]).append(item.data(Qt.ItemDataRole.UserRole))
        self.state.preview_order.update({folder:order for folder,order in folders.items() if folder is not None})
        self.change()

    def _refresh_requested(self):
        if not self._task_busy():
            self.preview_coordinator.invalidate()
            self.state.revision += 1
            self.preview_status.setText(tr('正在重新扫描文件…'))
            self.refresh()

    def _sort_changed(self,*args):
        if not self._loading:
            self.state.preview_order.clear()
            self.change()

    def _apply_naming_preset(self,index):
        template = self.naming_preset.itemData(index)
        if template is None or self._task_busy():
            return
        row = self.naming.table.currentRow()
        if row<0:
            self.naming.add_rule('*',template)
        else:
            self.naming.table.item(row,1).setText(template)

    def _browse_folder(self,field):
        path = QFileDialog.getExistingDirectory(self,tr('选择文件夹'),field.text(),
            QFileDialog.Option.ShowDirsOnly|QFileDialog.Option.DontUseNativeDialog)
        if path:
            field.setText(path)

    def _build_pickers(self,*args):
        while self.preset_grid.count():
            item = self.preset_grid.takeAt(0)
            if item.widget():
                if item.widget() is not self.preset_category:
                    item.widget().deleteLater()
        self._pickers = {}
        for index,ext in enumerate(EXT_CATEGORIES.get(self.preset_category.currentData(),())):
            checkbox = StateCheckBox(ext)
            self._pickers[ext] = checkbox
            checkbox.toggled.connect(lambda checked,ext=ext:self._picker_toggled(ext,checked))
            self.preset_grid.addWidget(checkbox,index//4+(1 if getattr(self,'_polished',False) else 0),index%4)
        if getattr(self,'_polished',False):
            self.preset_grid.addWidget(self.preset_category,0,0,1,4)
        self._sync_pickers()

    def _sync_pickers(self):
        enabled = {ext.strip().lower() for index,(ext,_) in enumerate(self.state.mappings)
                   if index>=len(self.state.mapping_enabled) or self.state.mapping_enabled[index]}
        for ext,checkbox in self._pickers.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(ext in enabled)
            checkbox.blockSignals(False)

    def _picker_toggled(self,ext,checked):
        if self._task_busy():
            return
        for row in range(self.mapping_table.rowCount()):
            if self.mapping_table.item(row,0).text().strip().lower()==ext:
                self.mapping_table.item(row,0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                return
        if checked:
            self._insert_mapping(ext,self.preset_category.currentText())

    @structural(tr('添加分类映射'))
    def _insert_mapping(self,ext,folder):
        self._loading = True
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)
        self.mapping_table.setItem(row,0,self._checked_item(ext,True))
        self.mapping_table.setItem(row,1,QTableWidgetItem(folder))
        self._loading = False
        self.change()
        return row

    def add_mapping(self):
        if not self._task_busy():
            row = self._insert_mapping('','')
            self.mapping_table.setCurrentCell(row,0)
            self.mapping_table.editItem(self.mapping_table.item(row,0))

    @structural(tr('删除分类映射'))
    def remove_mapping(self):
        if not self._task_busy():
            super().remove_mapping()

    @structural(tr('清空分类映射'))
    def _clear_mappings(self):
        if not self._task_busy():
            self.mapping_table.setRowCount(0)
            self.change()

    def _map_all(self):
        if self._task_busy():
            return
        self._map_all_pending = True
        self._refresh_requested()

    @structural(tr('映射全部文件类型'))
    def _apply_all_extensions(self,extensions):
        existing = {self.mapping_table.item(row,0).text().strip().lower():row for row in range(self.mapping_table.rowCount())}
        self._loading = True
        for ext in extensions:
            name = ext or next((key for key in ('[无后缀]', '[no extension]') if key in existing),
                               tr('[无后缀]'))
            if name in existing:
                self.mapping_table.item(existing[name],0).setCheckState(Qt.CheckState.Checked)
                continue
            folder = next((tr(category) for category,values in EXT_CATEGORIES.items() if ext in values),ext.lstrip('.') or tr('无后缀'))
            row = self.mapping_table.rowCount()
            self.mapping_table.insertRow(row)
            self.mapping_table.setItem(row,0,self._checked_item(name,True))
            self.mapping_table.setItem(row,1,QTableWidgetItem(folder))
        self._loading = False
        self.change()

    def _build_config(self):
        self.change()
        return self.state.to_config(True)

    def _run(self):
        if self._task_busy():
            return
        try:
            config = self._build_config()
        except (ValueError,TypeError) as error:
            self._task_error(str(error))
            return
        self.log.clear()
        self.progress_bar.setValue(0)
        self._save_settings()
        self.controller.execute(config,self.state.revision,self._confirm_config)

    def _summary_text(self,config,result):
        lines = [tr('输入目录：{0}', config["src"]),tr('输出目录：{0}', config["dest"]),
            tr('匹配 {0} 个文件；未匹配 {1} 个文件。', len(result.entries), result.total_files-len(result.entries)),
            tr('原始文件：')+(tr('保留（复制）') if config.get('keep_files') else tr('移动到输出目录')),
            tr('目录层级：')+(tr('保留') if config.get('keep_hierarchy') else tr('分类内平铺')),
            tr('文件对应清单：')+(tr('输出') if config.get('output_list') else tr('不输出'))]
        if config['first_compress']:
            lines.append(tr('压缩工具：')+TOOL_NAMES[config.get('archive_tool','bandizip')])
            tool = config.get('_archive_tool')
            if tool:
                lines.append(tr('命令行程序：')+tool.executable)
            for index,layer in enumerate(config['compression_layers']):
                lines.append(tr('第 {0} 层：{1} · 每包 {2} ', index+1, layer.archive_format.upper(), layer.group_size)+
                    (tr('文件') if index==0 else tr('完整单元'))+tr(' · 模板 {0}', layer.name_template)+
                    (tr(' · 分卷 ')+str(layer.volume) if layer.enable_volume else '')+
                    (tr(' · 已设置密码') if layer.password else '')+(tr(' · 校验') if layer.verify else '')+
                    (tr(' · 后缀 ')+layer.suffix if layer.suffix else ''))
            lines.append(tr('最外层 {0} 个完整单元；分卷数量及压缩后大小执行后确定。', len(result.plan.final_ids)))
        else:
            lines.append(tr('未启用压缩。'))
        return '\n'.join(lines)

    def review_summary(self):
        try:
            self.state.file_entries()
            config = self.state.to_config(True)
            text = self._summary_text(config,self.state.preview)
            if self.state.preview.archive_error:
                text += tr('\n\n无法执行：')+self.state.preview.archive_error
        except ValueError as error:
            text = str(error)
        QMessageBox.information(self,tr('参数摘要'),text)

    def _confirm_config(self,result):
        if not self.state.confirm_config:
            return True
        return QMessageBox.question(self,tr('确认执行'),self._summary_text(result.prepared,result)+
            tr('\n\n将按这份预检快照执行，是否开始？'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes

    @Slot(bool)
    def _set_task_active(self,active):
        if active:
            self.timer.stop()
            self.preview_service.cancel()
        for widget in (self.source_container,self.destination_container,self.recursive_cb,self.compression_cb,
                self.refresh_btn,self.mapping_page,self.naming,self.final_page,self.settings_btn,self.review_btn):
            widget.setEnabled(not active)
        self.run_btn.setEnabled(not active)
        self.resume_btn.setEnabled(not active)
        self.cancel_btn.setEnabled(active)
        self._sync_compression_ui()
        self._update_drag_enabled()

        self._sync_action_states()

    @Slot(str)
    def _append_log(self,text):
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(text)

    @Slot(int,int,str)
    def _update_progress(self,value,maximum,text):
        self.progress_bar.setRange(0,max(1,maximum))
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(text+' [%p%]')

    @Slot(str)
    def _task_error(self,message):
        self._append_log(tr('[错误] ')+message+'\n')
        self.log_toggle.setChecked(True)
        if not self._closing:
            show_styled_dialog(self,tr('无法完成任务'),message,width=480,height=260)

    @Slot(object,str,str)
    def _task_finished(self,stats,status,destination):
        if self._cancel_dlg:
            self._cancel_dlg.reject()
        if status!='completed':
            labels = dict(error=tr('处理失败'),partial=tr('部分分组跳过，保留断点'),safe_cancelled=tr('已安全中断'),force_cancelled=tr('已强制中断'))
            self.progress_bar.setFormat(labels.get(status,tr('任务已停止'))+' [%p%]')
        if status=='error':
            self.log_toggle.setChecked(True)
        self._save_settings()
        if not self._closing:
            show_stats_dialog(self,stats,dest_path=destination,status=status)

    @Slot()
    def _task_idle(self):
        if self._cancel_dlg:
            self._cancel_dlg.reject()
        if self._resume_saved_state is not None:
            self.state = self._resume_saved_state
            self._resume_saved_state = None
            self._resume_inspection = None
            self._populate_controls()
        if self._closing:
            QTimer.singleShot(0,self.close)
        else:
            self.preview_coordinator.invalidate()
            self.state.revision += 1
            self.refresh()

    def _cancel(self):
        if self._task_busy():
            self.controller.cancel()
            self.cancel_btn.setEnabled(False)
            self._sync_action_states()

    @Slot()
    def _on_cancel_waiting(self):
        if self._cancel_dlg is not None or self._closing:
            return
        dialog = QDialog(self)
        self._cancel_dlg = dialog
        dialog.setWindowTitle(tr('等待安全中断'))
        layout = QVBoxLayout(dialog)
        label = QLabel(tr('正在等待当前压缩或文件阶段完成并保存断点。\n强制中断会保留半成品，恢复时重试未完成组。'))
        label.setWordWrap(True)
        layout.addWidget(label)
        force = QPushButton(tr('强制中断'))
        force.clicked.connect(lambda:self.controller.cancel(force=True))
        force.clicked.connect(dialog.accept)
        layout.addWidget(force)
        dialog.finished.connect(lambda:self._clear_cancel_dialog(dialog))
        dialog.setMinimumWidth(420)
        dialog.open()

    def _clear_cancel_dialog(self,dialog):
        if self._cancel_dlg is dialog:
            self._cancel_dlg = None
        dialog.deleteLater()

    def _import_resume(self):
        path,_ = QFileDialog.getOpenFileName(self,tr('选择断点文件'),self.destination.text(),
            tr('SortZip 断点 (*.json)'),options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._start_resume(path)

    def _start_resume(self,path):
        if not self._task_busy():
            self.change()
            self.log.clear()
            self.controller.resume(str(path),self._resume_parameters,tool_paths=self.state.archive_tool_paths)

    def _resume_parameters(self,result):
        if self._closing:
            return None
        destination = str(Path(result.path).parent)
        approval = {}
        if result.input_root:
            if not self._confirm_resume(tr('恢复需要访问原输入目录：\n{0}\n请确认目录和文件属于这次任务。', result.input_root)):
                return None
            approval['approved_input_root'] = result.input_root
        if result.kind=='file_task':
            if not self._confirm_resume(tr('分类与改名断点：{0}\n共 {1} 个文件，继续未完成的操作？', result.path, len(result.groups))):
                return None
            return dict(resume_from=result.path,dest=destination,**approval)
        tool = result.tool_config.get('_archive_tool')
        tool_label = TOOL_NAMES[result.archive_tool] + (f'（{tool.executable}）' if tool else '')
        if result.kind=='multilayer':
            passwords = {}
            for level in result.encrypted_layers:
                dialog = PasswordDialog(self,initial=self._saved_password,hint=tr('请输入第 {0} 层原任务密码。密码仅保留在本次会话。', level+1))
                if dialog.exec()!=QDialog.DialogCode.Accepted:
                    return None
                passwords[level] = dialog.password()
                if not passwords[level]:
                    self._task_error(tr('第 {0} 层需要原任务密码', level+1))
                    return None
            total,done = len(result.plan.tasks),result.completed
            unverified = ''
            if result.unverified_layers:
                levels = '、'.join(str(index+1) for index in result.unverified_layers)
                unverified = tr('\n旧断点第 {0} 层没有已完成归档可验证密码。请确认输入的是原密码。', levels)
            if not self._confirm_resume(tr('多层断点：{0}\n压缩工具：{1}\n共 {2} 组，已完成 {3} 组，待处理 {4} 组。\n复用已完成产物；未完成组写入新的重试目录。', result.path, tool_label, total, done, total-done)+unverified):
                return None
            self._show_resume_plan(result,passwords)
            return dict(resume_from=result.path,layer_passwords=passwords,
                        dest=destination,**result.tool_config,**approval)
        total,done = len(result.groups),result.completed
        if done==total:
            if QMessageBox.question(self,tr('任务已完成'),tr('所有分组均已完成。是否清除此断点文件？'),
                    QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes:
                Path(result.path).unlink()
            return None
        if result.problems and not self._confirm_resume(tr('文件检查发现以下问题：\n')+'\n'.join(result.problems)+tr('\n仍然继续恢复？')):
            return None
        cfg = result.config
        unverified = ''
        if result.unverified_layers:
            levels = '、'.join(str(index+1) for index in result.unverified_layers)
            unverified = tr('\n旧断点第 {0} 层没有可用于验证的归档，请确认稍后输入的是原密码。', levels)
        if not self._confirm_resume(tr('断点：{0}\n待处理 {1} / {2} 组。\n压缩工具：{3}\n压缩格式：{4}\n恢复以断点配置为准；当前编辑草稿不会用于恢复。', result.path, total-done, total, tool_label, cfg.get("archive_format","zip").upper())+unverified):
            return None
        password = ''
        if cfg.get('password'):
            dialog = PasswordDialog(self,initial=self._saved_password,hint=tr('输入原任务密码。'))
            if dialog.exec()!=QDialog.DialogCode.Accepted:
                return None
            password = dialog.password()
            if not password:
                self._task_error(tr('请输入原任务密码'))
                return None
        self.notice.setText(tr('正在恢复旧版断点；编辑区显示原草稿，只读。实际执行以断点为准。'))
        return dict(resume_from=result.path,password=password,bandizip=tool.executable if tool else 'bandizip',
                    dest=destination,**result.tool_config)

    def _confirm_resume(self,text):
        return QMessageBox.question(self,tr('确认恢复'),text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes

    def _show_resume_plan(self,result,passwords):
        # The checkpoint owns archive identities. Never regenerate it from UI node keys.
        state = WorkspaceState(source=tr('断点中的已分类文件'),destination=str(Path(result.path).parent),
            keep_files=True,dark_mode=self.state.dark_mode,archive_tool=result.archive_tool)
        levels = max(task.layer_index for task in result.plan.tasks)+1
        state.layers = [draft_from_layer(replace(next(t.settings for t in result.plan.tasks if t.layer_index==index),
            password=passwords.get(index,''))) for index in range(levels)]
        entries = []
        for identity,paths in result.available.items():
            if identity.startswith('source:'):
                task = next((t for t in result.plan.tasks if identity in t.input_ids),None)
                if task:
                    entries.append(dict(source=identity[len('source:'):],folder=task.unit.folder,target=Path(paths.files[0]).name))
        state.preview = PreviewResult(state.revision,entries=entries,plan=result.plan,total_files=len(entries))
        state.selected = 'final'
        self._resume_saved_state,self.state = self.state,state
        self._resume_inspection = result
        self._populate_controls()
        self._render_preview()
        self.notice.setText(tr('恢复视图：按断点中的原始计划执行，配置只读；原编辑草稿将在任务结束后恢复。'))
        self._set_task_active(True)

    def toggle_theme(self):
        super().toggle_theme()
        self.state.dark_mode = self._dark
        if self._resume_saved_state is not None:
            self._resume_saved_state.dark_mode = self._dark

    def show_settings(self):
        if self._task_busy():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr('SortZip 设置'))
        layout = QVBoxLayout(dialog)
        preference = language_preference(self.settings)
        language = QuietFocusComboBox()
        language.setObjectName('InterfaceLanguage')
        for code,label in LANGUAGE_CHOICES:
            language.addItem(label,code)
        language.setCurrentIndex(language.findData(preference))
        language_form = QFormLayout()
        language_form.addRow(tr('界面语言'),language)
        layout.addLayout(language_form)
        restart_hint = QLabel(tr('重启 SortZip 后生效。'))
        restart_hint.setWordWrap(True)
        restart_hint.hide()
        language.currentIndexChanged.connect(lambda:restart_hint.setVisible(language.currentData()!=preference))
        layout.addWidget(restart_hint)
        confirm = StateCheckBox(tr('开始前显示配置确认'))
        confirm.setChecked(self.state.confirm_config)
        layout.addWidget(confirm)
        paths = QFormLayout()
        tool_fields = {}
        for identifier,name in TOOL_NAMES.items():
            field = QLineEdit(self.state.archive_tool_paths.get(identifier,''))
            detected = detect_tool(identifier)
            missing = tr('未检测到，请选择 bz.exe') if identifier=='bandizip' else tr('未检测到，请选择程序')
            field.setPlaceholderText(detected or missing)
            field.setToolTip(detected or missing)
            browse = QPushButton(tr('浏览'))
            def choose(checked=False,edit=field,title=name):
                selected,_ = QFileDialog.getOpenFileName(dialog,tr('选择 {0} 命令行程序',title),edit.text(),
                    tr('程序 (*.exe)'),options=QFileDialog.Option.DontUseNativeDialog)
                if selected:
                    edit.setText(selected)
            browse.clicked.connect(choose)
            verify = QPushButton(tr('检测'))
            def probe(checked=False,edit=field,tool=identifier):
                try:
                    path = check_tool(tool,{tool:edit.text().strip()})
                except (ValueError,OSError) as error:
                    show_styled_dialog(dialog,tr('压缩工具检测'),str(error))
                else:
                    show_styled_dialog(dialog,tr('压缩工具检测'),tr('检测通过\n{0}', path))
            verify.clicked.connect(probe)
            row = QHBoxLayout()
            row.addWidget(field,1)
            row.addWidget(browse)
            row.addWidget(verify)
            paths.addRow(name,row)
            tool_fields[identifier] = field
        layout.addLayout(paths)
        label = QLabel(tr('各层密码仅保留在当前会话；关闭软件后需重新输入。'))
        label.setWordWrap(True)
        layout.addWidget(label)
        clear_password = QPushButton(tr('清除旧版保存的密码'))
        clear_password.clicked.connect(self._clear_saved_password)
        layout.addWidget(clear_password)
        project = QPushButton(tr('GitHub 项目主页'))
        project.clicked.connect(lambda:QDesktopServices.openUrl(QUrl('https://github.com/WindySumm/SortZip')))
        layout.addWidget(project)
        reset = QPushButton(tr('恢复默认设置'))
        reset.clicked.connect(lambda:self._reset_settings(dialog))
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.settings.setValue(LANGUAGE_KEY,language.currentData())
            self.state.confirm_config = confirm.isChecked()
            self.state.archive_tool_paths = {identifier:field.text().strip() for identifier,field in tool_fields.items() if field.text().strip()}
            self._save_settings()

    def _clear_saved_password(self):
        if save_password(self.settings,self.credential_store,'',False,self._append_log):
            self.settings.setValue('remember_pwd',False)
            self._saved_password = ''
            self._append_log(tr('已清除旧版保存密码。各层会话密码未改动。\n'))

    def _reset_settings(self,dialog=None):
        if not self._confirm_resume(tr('恢复默认设置会清空当前配置和已保存偏好，是否继续？')):
            return
        self._clear_saved_password()
        language = language_preference(self.settings)
        self.settings.clear()
        self.settings.setValue(LANGUAGE_KEY,language)
        self.structure_history.stack.clear()
        self.state = WorkspaceState(revision=self.state.revision+1)
        self._populate_controls()
        self.preview_coordinator.invalidate()
        self.refresh()
        self._save_settings()
        if dialog:
            dialog.reject()

    def _save_settings(self):
        state = self._resume_saved_state or self.state
        state.save(self.settings)
        self.settings.setValue('workbench_geometry',self.saveGeometry())
        self.settings.sync()

    def _on_update_result(self,latest):
        from sortzip_core.app import _compare_versions
        if not self._closing and latest and _compare_versions(latest,__version__):
            if self._confirm_resume(tr('发现新版本 {0}，是否打开发布页面？', latest)):
                QDesktopServices.openUrl(QUrl('https://github.com/WindySumm/SortZip/releases/latest'))

    def closeEvent(self,event):
        self._closing = True
        self.timer.stop()
        self.preview_service.cancel()
        if self._task_busy():
            event.ignore()
            self.controller.cancel()
            self._append_log(tr('等待安全停止并释放线程后关闭。\n'))
            return
        checker = getattr(self,'_update_checker',None)
        if self.preview_service.busy() or self.controller.planner.busy() or (checker is not None and checker.isRunning()):
            event.ignore()
            if checker is not None:
                checker.requestInterruption()
            QTimer.singleShot(80,self.close)
            return
        self.save_layer()
        self._save_settings()
        event.accept()
