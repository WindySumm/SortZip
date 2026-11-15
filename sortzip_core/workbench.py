"""Production flow workbench: shared editors, real plans and task controller."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt,QTimer,QSettings,QSignalBlocker,QUrl,Slot
from PySide6.QtGui import QIcon,QDesktopServices,QTextCursor
from PySide6.QtWidgets import (QApplication,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPushButton,QFileDialog,QDialog,QDialogButtonBox,QTableWidgetItem,
    QMessageBox,QGridLayout,QSizePolicy,QLineEdit,QScrollArea)

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
        self.keep.setText(tr('保留输入目录中的原始文件'))
        final_layout = self.final_page.layout()
        index = final_layout.indexOf(self.keep)
        self.retention_heading = QLabel(tr('保留产物'))
        self.retention_heading.setObjectName('SectionTitle')
        final_layout.insertWidget(index,self.retention_heading)
        shortcuts = QHBoxLayout()
        for label,keep in ((tr('仅保留最外层'),False),(tr('全部保留'),True)):
            button = QPushButton(label)
            button.clicked.connect(lambda checked,value=keep:self._set_retention(value))
            shortcuts.addWidget(button)
        shortcuts.addStretch()
        self.retention_toggle = QPushButton()
        self.retention_toggle.setObjectName('RetentionSummary')
        self.retention_toggle.setCheckable(True)
        disclosure_layout = QHBoxLayout(self.retention_toggle)
        disclosure_layout.setContentsMargins(0,0,10,0)
        disclosure_layout.addStretch()
        self.retention_arrow = QLabel('▸')
        self.retention_arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        disclosure_layout.addWidget(self.retention_arrow)
        self.retention_options = QWidget()
        self.retention_layout = QVBoxLayout(self.retention_options)
        self.retention_layout.setContentsMargins(6,0,10,0)
        self.retention_layout.setSpacing(4)
        self.retention_scroll = QScrollArea()
        self.retention_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.retention_scroll.setWidgetResizable(True)
        self.retention_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.retention_scroll.setWidget(self.retention_options)
        self.retention_toggle.toggled.connect(self._show_retention_rows)
        final_layout.insertLayout(index+1,shortcuts)
        final_layout.insertWidget(index+3,self.retention_toggle)
        final_layout.insertWidget(index+4,self.retention_scroll)
        self.retention_shortcuts = shortcuts
        self.restore_original_names = StateCheckBox(tr('随包附带原始文件名还原脚本'))
        self.restore_original_names.setToolTip(tr('手动解压各组后运行脚本，可校验文件并恢复原名。随包映射包含原始文件名，请按需启用。'))
        final_layout.insertWidget(final_layout.indexOf(self.output_list)+1,self.restore_original_names)
        self.restore_original_names.toggled.connect(self.change)
        unpack_heading = QLabel(tr('SortZip 解包并恢复'))
        unpack_heading.setObjectName('SectionTitle')
        self.delivery_manifest = StateCheckBox(tr('生成自动解包清单'))
        self.delivery_manifest.setToolTip(tr('生成供 SortZip 自动解压使用的 .sortzip.json。分享清单和需要交付的完整外层分卷，并保留相对位置。清单会公开归档结构、文件名和相对目录。'))
        self.delivery_original = StateCheckBox(tr('自动恢复原文件名'))
        self.delivery_original.setToolTip(tr('将原名写入自动解包清单，供 SortZip 解包后还原。分享清单就会公开原名。手动改名脚本和 List.txt 由各自的选项控制。'))
        for offset,control in enumerate((unpack_heading,self.delivery_manifest,self.delivery_original),1):
            final_layout.insertWidget(final_layout.indexOf(self.restore_original_names)+offset,control)
        for control in (self.delivery_manifest,self.delivery_original):
            control.toggled.connect(self.change)
        self._retention_layer_keys = ()
        self._retention_checkboxes = []
        self.archive_tool_combo.currentIndexChanged.connect(self._archive_tool_changed)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.edit_scroll.setMinimumHeight(130)
        self.preview_tabs.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        self.controller = TaskController(self,factory=task_factory)
        self.controller.active.connect(self._set_task_active)
        self.controller.unpack_password_requested.connect(self._answer_unpack_password)
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
            if name=='destination':
                field.setToolTip(tr('整理后的文件和压缩包放在这里。可选输入的上一级文件夹，结果会放在输入文件夹外。'))
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
                self.mapping_table,self.naming.table,self.naming,self.archive_tool_combo,self.restore_original_names,
                self.delivery_manifest,self.delivery_original):
            self.source.setText(self.state.source)
            self.destination.setText(self.state.destination)
            for widget,value in ((self.recursive_cb,self.state.recursive),(self.compression_cb,self.state.compression_enabled),
                    (self.keep_hierarchy_cb,self.state.keep_hierarchy),(self.keep,self.state.keep_files),(self.output_list,self.state.output_list)):
                widget.setChecked(value)
            self.sort_combo.setCurrentIndex(max(0,self.sort_combo.findData(self.state.sort_by)))
            self.archive_tool_combo.setCurrentIndex(self.archive_tool_combo.findData(self.state.archive_tool))
            self.restore_original_names.setChecked(self.state.restore_original_names)
            self.delivery_manifest.setChecked(self.state.delivery_manifest)
            self.delivery_original.setChecked(self.state.delivery_original_names)
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
            self.state.restore_original_names = self.restore_original_names.isChecked()
            self.state.delivery_manifest = self.delivery_manifest.isChecked()
            self.state.delivery_original_names = self.delivery_original.isChecked()
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
        editable = not self._task_busy()
        enabled = self.state.compression_enabled and editable
        self.archive_tool_combo.setEnabled(enabled)
        self.keep.setEnabled(editable)
        self.output_list.setEnabled(editable)
        self.restore_original_names.setEnabled(enabled)
        self.delivery_manifest.setEnabled(enabled)
        self.delivery_original.setEnabled(enabled and self.state.delivery_manifest)
        for index in range(self.retention_shortcuts.count()-1):
            button = self.retention_shortcuts.itemAt(index).widget()
            button.setVisible(self.state.compression_enabled)
            button.setEnabled(enabled)
        self._refresh_retention(enabled)
        self.layer_editor.setEnabled(enabled)
        self.add_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled and self.draft() is not None)
        self.remove_btn.setEnabled(enabled and self.draft() is not None and len(self.state.layers)>1)
        self._sync_action_states()

    def _set_retention(self,keep,through=None):
        if self._task_busy():
            return
        for index,draft in enumerate(self.state.layers[:-1]):
            if through is None or index<=through:
                draft.data['retain'] = keep
        if through is None:
            with loading_controls(self,self.keep):
                self.keep.setChecked(keep)
        self.change()

    def _toggle_retained_layer(self,index,checked):
        self.state.layers[index].data['retain'] = checked
        self.change()

    def _show_retention_rows(self):
        expanded = self.retention_toggle.isChecked()
        self.retention_arrow.setText('▾' if expanded else '▸')
        count = len(self.state.layers)
        self.retention_toggle.setVisible(self.state.compression_enabled and count>1)
        self.retention_scroll.setVisible(self.state.compression_enabled and (count==1 or expanded))

    def _refresh_retention(self,editable):
        keys = tuple(draft.key for draft in self.state.layers)
        if keys!=self._retention_layer_keys:
            self._retention_layer_keys = keys
            self._rebuild_retention_rows(len(keys))
        retained = 1
        for draft,checkbox in zip(self.state.layers[:-1],self._retention_checkboxes):
            checked = bool(draft.data.get('retain'))
            with QSignalBlocker(checkbox):
                checkbox.setChecked(checked)
            retained += checked
        self.retention_toggle.setText(tr('归档 · 保留 {0} / {1} 层',retained,len(keys)))
        self.retention_options.setEnabled(editable)
        self._show_retention_rows()

    def _rebuild_retention_rows(self,count):
        self._retention_checkboxes.clear()
        while self.retention_layout.count():
            row = self.retention_layout.takeAt(0).widget()
            row.hide()
            row.deleteLater()
        row_height = 32
        for index in range(count):
            row = QWidget()
            row.setFixedHeight(row_height)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0,0,0,0)
            outer = index==count-1
            checkbox = StateCheckBox(tr('第 {0} 层（最外层）',index+1) if outer else tr('第 {0} 层',index+1))
            if outer:
                checkbox.setChecked(True)
                checkbox.setEnabled(False)
            else:
                checkbox.toggled.connect(lambda checked,i=index:self._toggle_retained_layer(i,checked))
                self._retention_checkboxes.append(checkbox)
            layout.addWidget(checkbox)
            layout.addStretch()
            if not outer:
                button = QPushButton(tr('保留此层及内层'))
                button.setObjectName('QuietAction')
                button.clicked.connect(lambda checked,i=index:self._set_retention(True,i))
                layout.addWidget(button)
            self.retention_layout.addWidget(row)
        visible = min(count,5)
        self.retention_scroll.setFixedHeight(visible*row_height+max(0,visible-1)*self.retention_layout.spacing())

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
        result = self.state.preview
        if result is None:
            return
        self.notice.setText(tr('已扫描 {0} 个文件，匹配 {1} 个。', result.total_files, len(result.entries))+
            (tr('存在未匹配类型，可使用“映射全部”。') if result.total_files>len(result.entries) else tr('预览只读，确认执行后才处理文件。')))
        self.summary.setText(tr('输出目录：{0}\n\n匹配 {1} 个文件。', self.state.destination or tr('尚未选择'), len(result.entries))+
            (tr('共 {0} 层，最终生成 {1} 个压缩包。', len(self.state.layers), len(result.plan.final_ids)) if self.state.compression_enabled
             else tr('未启用压缩，只执行分类、命名与可选清单。')))
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
        return dict(self.state.to_config(True),managed_recovery=True)

    def _run(self):
        if self._task_busy():
            return
        try:
            config = self._build_config()
        except (ValueError,TypeError) as error:
            self._task_error(str(error))
            return
        if (config.get('delivery_manifest') and config.get('archive_tool') in ('bandizip','7zip')
                and any(layer.password for layer in config.get('compression_layers',()))):
            mode = self._choose_delivery_password_mode()
            if mode is None:
                return
            if mode=='encrypted':
                phrase = self._delivery_phrase()
                if phrase is None:
                    return
                config.update(delivery_password_mode='encrypted',delivery_phrase=phrase)
            elif mode=='plain':
                if not self._confirm_resume(tr('任何拿到总清单的人都能读取压缩密码。确定明文附带？')):
                    return
                config['delivery_password_mode'] = 'plain'
        self.log.clear()
        self.progress_bar.setValue(0)
        self._save_settings()
        self.controller.execute(config,self.state.revision,self._confirm_config)

    def _choose_delivery_password_mode(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(tr('交付密码'))
        layout = QVBoxLayout(dialog)
        label = QLabel(tr('选择密码交付方式'))
        selector = QuietFocusComboBox(dialog)
        selector.setProperty('softHover',True)
        selector.setAttribute(Qt.WidgetAttribute.WA_Hover)
        selector.setSizeAdjustPolicy(QuietFocusComboBox.SizeAdjustPolicy.AdjustToContents)
        label.setBuddy(selector)
        for mode,text in (('none',tr('不附带密码')),('encrypted',tr('用交付口令加密')),('plain',tr('明文附带密码'))):
            selector.addItem(text,mode)
        layout.addWidget(label)
        layout.addWidget(selector)
        selector.ensurePolished()
        metrics = selector.fontMetrics()
        selector.setMinimumWidth(max(metrics.horizontalAdvance(selector.itemText(i)) for i in range(selector.count()))+36)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        mode = selector.currentData() if dialog.exec()==QDialog.DialogCode.Accepted else None
        dialog.deleteLater()
        return mode

    def _delivery_phrase(self):
        dialog = PasswordDialog(self,hint=tr('请将交付口令与清单分开传递。'),delivery=True)
        phrase = dialog.password() if dialog.exec()==QDialog.DialogCode.Accepted else None
        dialog.deleteLater()
        return phrase

    def _summary_text(self,config,result):
        lines = [tr('输入目录：{0}', config["src"]),tr('输出目录：{0}', config["dest"]),
            tr('匹配 {0} 个文件；未匹配 {1} 个文件。', len(result.entries), result.total_files-len(result.entries)),
            tr('原始文件：')+(tr('保留（复制）') if config.get('keep_files') else (
                tr('移动并在归档后清理工作文件') if config['first_compress'] else tr('移动到输出目录'))),
            tr('目录层级：')+(tr('保留') if config.get('keep_hierarchy') else tr('分类内平铺')),
            tr('文件对应清单：')+(tr('输出 List.txt（含原相对路径及新文件名，分享前请核对）') if config.get('output_list') else tr('不输出'))]
        if config['first_compress']:
            retained = [index+1 for index in config.get('retain_layers',())]
            lines.append(tr('保留归档层：{0}', ', '.join(map(str, [*retained,len(config['compression_layers'])]))))
            if config.get('restore_original_names'):
                lines.append(tr('随包附带原始文件名还原脚本'))
            if config.get('delivery_manifest'):
                lines.append(tr('SortZip 解包并恢复'))
                lines.extend((tr('生成自动解包清单'),tr('包外清单会公开归档结构、文件名和相对目录。')))
                if config.get('delivery_original_names'):
                    lines.append(tr('自动恢复原文件名'))
            lines.append(tr('压缩工具：')+TOOL_NAMES[config.get('archive_tool','bandizip')])
            tool = config.get('_archive_tool')
            if tool:
                lines.append(tr('命令行程序：')+tool.executable)
            for index,layer in enumerate(config['compression_layers']):
                automatic_verify = index==0 or (index-1 not in config.get('retain_layers',()))
                lines.append(tr('第 {0} 层：{1} · 每包 {2} ', index+1, layer.archive_format.upper(), layer.group_size)+
                    (tr('文件') if index==0 else tr('压缩包'))+tr(' · 模板 {0}', layer.name_template)+
                    (tr(' · 分卷 ')+str(layer.volume) if layer.enable_volume else '')+
                    (tr(' · 已设置密码') if layer.password else '')+(tr(' · 校验') if layer.verify or automatic_verify else '')+
                    (tr(' · 后缀 ')+layer.suffix if layer.suffix else ''))
            lines.append(tr('最终生成 {0} 个压缩包。分卷数量和大小以实际结果为准。', len(result.plan.final_ids)))
        else:
            lines.append(tr('未启用压缩。'))
        if config.get('_space_report'):
            lines.extend(('',config['_space_report']))
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
        if not self.state.confirm_config and not result.prepared.get('_space_warning'):
            return True
        return QMessageBox.question(self,tr('确认执行'),self._summary_text(result.prepared,result)+
            tr('\n\n使用以上设置开始？'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes

    @Slot(bool)
    def _set_task_active(self,active):
        if active:
            self.timer.stop()
            self.preview_service.cancel()
        for widget in (self.source_container,self.destination_container,self.recursive_cb,self.compression_cb,
                self.refresh_btn,self.mapping_page,self.naming,self.settings_btn,self.review_btn):
            widget.setEnabled(not active)
        ready = not active and not self._closing
        self.run_btn.setEnabled(ready)
        self.commands['run'].setEnabled(ready)
        self.resume_btn.setEnabled(not active)
        self.cancel_btn.setEnabled(active)
        self.commands['cancel'].setEnabled(active)
        self.cancel_btn.setText(tr('取消'))
        self.task_buttons.setCurrentWidget(self.cancel_btn if active else self.run_btn)
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
        if not self._task_busy() or not self.cancel_btn.isEnabled():
            return
        self.cancel_btn.setText(tr('正在取消…'))
        self.cancel_btn.setEnabled(False)
        self.commands['cancel'].setEnabled(False)
        self.controller.cancel()

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
        from sortzip_core.file_journal import RECOVERY
        folder = Path(self.destination.text())
        managed = folder/RECOVERY
        start = str(managed) if self.destination.text() and managed.is_dir() else self.destination.text()
        path,_ = QFileDialog.getOpenFileName(self,tr('选择断点文件'),start,
            tr('SortZip 断点 (*.json)'),options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._start_resume(path)

    def _import_unpack(self):
        from sortzip_core.unpack_dialog import choose_task
        if self._task_busy():
            return
        config = choose_task(self,self.destination.text())
        if config:
            config['archive_tool_paths'] = dict(self.state.archive_tool_paths)
            self.log.clear()
            self.controller.launch(config)

    @Slot(object)
    def _answer_unpack_password(self,reply):
        from PySide6.QtWidgets import QInputDialog
        ready,_,layer,entry,ambiguous = reply
        try:
            if not self._closing and self._task_busy():
                location = tr('第 {0} 层：{1}',layer,entry) if layer else tr('最外层：{0}',entry)
                if ambiguous:
                    location += '\n\n'+tr('已试完密码清单。7-Zip 无法区分密码不符和归档数据异常。可输入新密码，或取消并检查归档。')
                value,accepted = QInputDialog.getText(self,tr('解包密码'),location,QLineEdit.EchoMode.Password)
                if accepted and value:
                    reply[1] = value
        finally:
            ready.set()

    def _start_resume(self,path):
        if not self._task_busy():
            self.change()
            self.log.clear()
            self.controller.resume(str(path),self._resume_parameters,tool_paths=self.state.archive_tool_paths)

    def _resume_parameters(self,result):
        if self._closing:
            return None
        from sortzip_core.file_journal import recovery_output
        destination = str(recovery_output(Path(result.path)))
        if result.kind=='cleanup':
            return dict(resume_from=result.path,dest=destination)
        approval = {}
        if result.input_root:
            if not self._confirm_resume(tr('恢复需要访问原输入目录：\n{0}\n请确认目录和文件属于这次任务。', result.input_root)):
                return None
            approval['approved_input_root'] = result.input_root
        if result.kind=='file_task':
            space = '\n\n'+result.space_report if result.space_report else ''
            if not self._confirm_resume(tr('分类与改名断点：{0}\n共 {1} 个文件，继续未完成的操作？', result.path, len(result.groups))+space):
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
            if result.space_report:
                unverified += '\n\n'+result.space_report
            if result.preserved_units:
                unverified += '\n\n'+tr('旧断点有 {0} 个输入单元未记录归档成员校验。现存文件将保留。',result.preserved_units)
            if not self._confirm_resume(tr('多层断点：{0}\n压缩工具：{1}\n共 {2} 组，已完成 {3} 组，待处理 {4} 组。\n复用已完成产物；未完成组写入新的重试目录。', result.path, tool_label, total, done, total-done)+unverified):
                return None
            self._show_resume_plan(result,passwords)
            delivery_options = {}
            if result.config.get('delivery_password_mode')=='encrypted':
                phrase = self._delivery_phrase()
                if phrase is None:
                    return None
                delivery_options['delivery_phrase'] = phrase
            return dict(resume_from=result.path,layer_passwords=passwords,**delivery_options,
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
        state.keep_files = result.config.get('keep_files',True)
        state.restore_original_names = result.config.get('restore_original_names',False)
        state.delivery_manifest = result.config.get('delivery_manifest',False)
        state.delivery_original_names = result.config.get('delivery_original_names',False)
        state.output_list = result.config.get('output_list',False)
        for index,draft in enumerate(state.layers[:-1]):
            draft.data['retain'] = index in result.config.get('retain_layers',range(levels-1))
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
