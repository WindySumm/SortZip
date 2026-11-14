"""Shared workbench layout and editors; no demo defaults or task startup."""
from copy import deepcopy

from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget,
    QListWidgetItem, QStackedWidget, QSplitter, QScrollArea, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QTextEdit, QStyledItemDelegate, QTabWidget, QGridLayout)

from sortzip_core.widgets import ReorderableTable,DropLineEdit
from sortzip_core import __version__
from sortzip_core.formats import LAYER_FORMATS as FORMATS
from sortzip_core.layer_editor import LayerRow
from sortzip_core.nesting import Layer
from sortzip_core.ui_controls import StateCheckBox,loading_controls
from sortzip_core.preview_display import render_filename_rows
from sortzip_core.workspace_state import LayerDraft
from sortzip_core.transient_scrollbars import TransientScrollBars
from sortzip_core.workbench_controls import FlowNavigationDelegate, NodeRole, QuietFocusComboBox, PreviewItemDelegate
from sortzip_core.i18n import tr



WORKBENCH_WIDE_WIDTH = 1100

class LiveRuleDelegate(QStyledItemDelegate):
    """Publish typing without ending the editing session or resetting its cursor."""
    def createEditor(self,parent,option,index):
        editor = super().createEditor(parent,option,index)
        if isinstance(editor,QLineEdit):
            editor.textEdited.connect(lambda text:self.commitData.emit(editor))
        return editor

    def setEditorData(self,editor,index):
        if isinstance(editor,QLineEdit) and editor.text()==index.data(Qt.ItemDataRole.EditRole):
            return
        super().setEditorData(editor,index)


class NamingEditor(QWidget):
    """One visual vocabulary, with token meanings scoped to the naming target."""
    changed = Signal()
    structureStarting = Signal(str)
    structureFinished = Signal()

    def __init__(self,mode,template=None,parent=None,*,production=False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        if mode=='files':
            self.table = (ReorderableTable if production else QTableWidget)(0,2)
            self.table.setItemDelegate(LiveRuleDelegate(self.table))
            self.table.setHorizontalHeaderLabels([tr('匹配分类目录'),tr('文件名称模板')])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.table.setMinimumHeight(140)
            layout.addWidget(self.table)
            if not production:
                controls = QHBoxLayout()
                add,remove = QPushButton(tr('＋ 添加规则')),QPushButton(tr('删除选中规则'))
                add.clicked.connect(lambda:self.add_rule('*','{original}{ext}'))
                remove.clicked.connect(self.remove_rule)
                controls.addWidget(add)
                controls.addWidget(remove)
                controls.addStretch()
                layout.addLayout(controls)
            self.table.itemChanged.connect(self.changed)
            text = tr('原文件：{n} 分类内序号 · {original} 原基础名 · {ext} 原扩展名 · {folder} 目录名。规则从上到下匹配，留空模板保留原名。')
        else:
            layout.addWidget(QLabel(tr('本层归档名称模板')))
            layout.addWidget(template)
            text = tr('压缩包：{n} 本层组序号 · {original} 本层输入单元范围 · {source_range} 原文件序号范围 · {folder} 目录名。扩展名由压缩格式附加。')
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setObjectName('Hint')
        layout.addWidget(hint)
        if mode=='files':
            layout.addStretch()
            self.fit_table()

    def fit_table(self):
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row,32)
        height = self.table.horizontalHeader().sizeHint().height()+min(8,max(2,self.table.rowCount()))*32+4
        self.table.setFixedHeight(height)

    def add_rule(self,folder,template):
        self.structureStarting.emit(tr('添加命名规则'))
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row,0,QTableWidgetItem(folder))
        self.table.setItem(row,1,QTableWidgetItem(template))
        self.fit_table()
        self.structureFinished.emit()

    def remove_rule(self):
        if self.table.currentRow()>=0:
            self.structureStarting.emit(tr('删除命名规则'))
            self.table.removeRow(self.table.currentRow())
            self.fit_table()
            self.changed.emit()
            self.structureFinished.emit()

    def clear_rules(self):
        if self.table.rowCount():
            self.structureStarting.emit(tr('清空命名规则'))
            self.table.setRowCount(0)
            self.fit_table()
            self.changed.emit()
            self.structureFinished.emit()

    def rules(self):
        return [(self.table.item(row,0).text() if self.table.item(row,0) else '',
                 self.table.item(row,1).text() if self.table.item(row,1) else '') for row in range(self.table.rowCount())]


class WorkbenchView(QWidget):
    def __init__(self,state,*,production=False):
        super().__init__()
        self.state = state
        if production: self._init_commands()
        self._loading = True
        self._dark = False
        self._preview_tabs = {}
        self._preview_has_issue = False
        self._filename_render_key = None
        self._tree_render_key = None
        self.setWindowTitle(tr('SortZip {0} · 流程工作台', __version__))
        self.setMinimumSize(640,600)
        self.resize(WORKBENCH_WIDE_WIDTH,800)
        root = QVBoxLayout(self)
        root.setContentsMargins(16,12,16,12)
        root.setSpacing(12)
        brand = QHBoxLayout()
        title = QLabel(tr('SortZip  /  流程工作台'))
        title.setObjectName('Brand')
        brand.addWidget(title)
        if not production:
            badge = QLabel(tr('交互原型 · 示例数据'))
            self.badge = badge
            badge.setObjectName('Badge')
            brand.addWidget(badge)
            brand.addStretch()
            self.theme_btn = QPushButton(tr('深色模式'))
            self.theme_btn.setToolTip(tr('切换本原型的浅色／深色外观'))
            self.theme_btn.clicked.connect(self.toggle_theme)
            guide = QPushButton(tr('原型说明'))
            self.guide_btn = guide
            brand.addWidget(self.theme_btn)
            brand.addWidget(guide)
        root.addLayout(brand)
        self.paths = QGridLayout()
        self.paths.setContentsMargins(0,0,0,0)
        self.paths.setHorizontalSpacing(8)
        self.paths.setVerticalSpacing(8)
        self.source = (DropLineEdit if production else QLineEdit)(self.state.source)
        self.destination = (DropLineEdit if production else QLineEdit)(self.state.destination)
        self.source_label,self.destination_label = QLabel(tr('输入目录')),QLabel(tr('输出目录'))
        self.source_label.setBuddy(self.source)
        self.destination_label.setBuddy(self.destination)
        self._paths_wide = None
        self.layout_paths()
        root.addLayout(self.paths)
        self.notice = QLabel(tr('预览只读，确认执行后才处理文件。'))
        self.notice.setWordWrap(True)
        self.notice.setObjectName('Hint')
        # Keep section captions outside the cards so their top edges share a row.
        self.navigation_heading = QLabel(tr('处理流程'))
        self.navigation_heading.setFixedWidth(180)
        section_heading = QHBoxLayout()
        section_heading.setContentsMargins(0,0,0,0)
        section_heading.setSpacing(16)
        section_heading.addWidget(self.navigation_heading)
        section_heading.addWidget(self.notice,1)
        root.addLayout(section_heading)
        self.outer = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.outer,1)
        navigation = QWidget()
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(0,0,0,0)
        self.navigation = QListWidget()
        self.navigation.setProperty('hideScrollIndicator',True)
        self.navigation.setMinimumWidth(176)
        self.navigation.setAccessibleName(tr('处理流程导航'))
        self.navigation.setItemDelegate(FlowNavigationDelegate(self.navigation))
        self.navigation.setWordWrap(True)
        nav_layout.addWidget(self.navigation,1)
        if not production:
            self.add_btn,self.copy_btn,self.remove_btn = QPushButton(tr('＋ 添加压缩层')),QPushButton(tr('复制本层')),QPushButton(tr('删除本层'))
            for button in (self.add_btn,self.copy_btn,self.remove_btn):
                nav_layout.addWidget(button)
        self.outer.addWidget(navigation)
        self.content = QSplitter(Qt.Orientation.Horizontal)
        self.content.setChildrenCollapsible(False)
        self.outer.addWidget(self.content)
        self.outer.setChildrenCollapsible(False)
        self.outer.setSizes([180,1040])
        self.outer.setStretchFactor(1,1)
        self.edit_scroll = QScrollArea()
        self.edit_scroll.setWidgetResizable(True)
        self.edit_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(12,0,12,0)
        self.editor_title = QLabel()
        self.editor_title.setObjectName('SectionTitle')
        editor_layout.addWidget(self.editor_title)
        self.pages = QStackedWidget()
        editor_layout.addWidget(self.pages)
        self.edit_scroll.setWidget(editor)
        self.content.addWidget(self.edit_scroll)
        self.mapping_page = QWidget()
        mapping_layout = QVBoxLayout(self.mapping_page)
        mapping_layout.setContentsMargins(0,0,0,0)
        tip = QLabel(tr('只选择需要处理的类型，将文件归入对应目录。修改分类名后，检查下一步命名规则是否仍能匹配。'))
        tip.setWordWrap(True)
        mapping_layout.addWidget(tip)
        self.mapping_table = (ReorderableTable if production else QTableWidget)(0,2)
        self.mapping_table.setHorizontalHeaderLabels([tr('文件类型'),tr('分类目录')])
        self.mapping_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for ext,folder in self.state.mappings:
            row = self.mapping_table.rowCount()
            self.mapping_table.insertRow(row)
            self.mapping_table.setItem(row,0,QTableWidgetItem(ext))
            self.mapping_table.setItem(row,1,QTableWidgetItem(folder))
        mapping_layout.addWidget(self.mapping_table)
        if not production:
            map_buttons = QHBoxLayout()
            map_add,map_remove = QPushButton(tr('＋ 添加类型')),QPushButton(tr('删除选中类型'))
            map_add.clicked.connect(self.add_mapping)
            map_remove.clicked.connect(self.remove_mapping)
            map_buttons.addWidget(map_add)
            map_buttons.addWidget(map_remove)
            map_buttons.addStretch()
            mapping_layout.addLayout(map_buttons)
        self.pages.addWidget(self.mapping_page)
        self.naming = NamingEditor('files',production=production)
        for folder,template in self.state.rules:
            self.naming.add_rule(folder,template)
        self.pages.addWidget(self.naming)
        self.layer_editor = LayerRow(Layer(),format_combo_factory=QuietFocusComboBox,actions=not production,inline_template=False)
        self.layer_editor.header.hide()
        if not production:
            self.layer_editor.copy_btn.hide()
            self.layer_editor.remove_btn.hide()
        # Reuse the same naming editor vocabulary without recreating input controls.
        self.archive_naming = NamingEditor('archive',self.layer_editor.template)
        self.layer_editor.layout().insertRow(2,self.archive_naming)
        self.layer_editor.advanced_toggle.setText(tr('密码设置 ▸'))
        self.layer_editor.advanced_toggle.toggled.connect(lambda checked:self.layer_editor.advanced_toggle.setText(tr('密码设置 ▾') if checked else tr('密码设置 ▸')))
        self.pages.addWidget(self.layer_editor)
        self.final_page = QWidget()
        final_layout = QVBoxLayout(self.final_page)
        final_layout.setContentsMargins(0,0,0,0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        final_layout.addWidget(self.summary)
        self.keep = StateCheckBox(tr('保留原始文件'))
        self.output_list = StateCheckBox(tr('输出文件对应清单'))
        final_layout.addWidget(self.keep)
        final_layout.addWidget(self.output_list)
        final_layout.addStretch()
        self.pages.addWidget(self.final_page)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(12,0,0,0)
        self.preview_title = QLabel()
        self.preview_title.setObjectName('SectionTitle')
        preview_layout.addWidget(self.preview_title)
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        self.tree = QTreeWidget()
        self.tree.setObjectName('StepPreview')
        self.tree.setItemDelegate(PreviewItemDelegate(self.tree))
        self.tree.setHeaderLabels([tr('输入 / 成员'),tr('输出 / 目标位置')])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0,190)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAccessibleName(tr('当前步骤输入输出预览'))
        self.filename_table = (ReorderableTable if production else QTableWidget)(0,4)
        self.filename_table.setObjectName('FilenamePreview')
        self.filename_table.setItemDelegate(PreviewItemDelegate(self.filename_table))
        self.filename_table.setHorizontalHeaderLabels([tr('原文件名'),tr('命名后'),tr('分类目录'),tr('所属最外层归档')])
        self.filename_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.filename_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.filename_table.setAlternatingRowColors(True)
        self.filename_table.setWordWrap(False)
        self.filename_table.setAccessibleName(tr('文件名实时预览：原文件名、命名后、分类目录、所属最外层归档'))
        self.filename_table.verticalHeader().hide()
        for column,width in enumerate((150,150,100,180)):
            self.filename_table.setColumnWidth(column,width)
        self.filename_table.horizontalHeader().setStretchLastSection(True)
        self.filename_tools = QWidget()
        tools_layout = QHBoxLayout(self.filename_tools)
        tools_layout.setContentsMargins(8,8,8,8)
        tools_layout.setSpacing(8)
        self.category_filter = QuietFocusComboBox()
        self.category_filter.addItem(tr('全部分类'))
        self.category_filter.setAccessibleName(tr('按分类筛选文件名预览'))
        self.category_filter.setMinimumWidth(100)
        tools_layout.addWidget(self.category_filter,0,Qt.AlignmentFlag.AlignVCenter)
        self.location_columns = StateCheckBox(tr('更多信息'))
        self.location_columns.setAccessibleName(tr('更多信息：显示分类目录与所属最外层归档'))
        self.location_columns.setToolTip(tr('显示分类目录和所属最外层归档；关闭后优先展示原名与新名'))
        tools_layout.addWidget(self.location_columns,0,Qt.AlignmentFlag.AlignVCenter)
        tools_layout.addStretch()
        self.filename_count = QLabel(tr('0 项'))
        self.filename_count.setMinimumWidth(72)
        self.filename_count.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        self.filename_count.setObjectName('Hint')
        tools_layout.addWidget(self.filename_count)
        self.category_filter.currentIndexChanged.connect(self.filter_filenames)
        self.location_columns.toggled.connect(self.set_location_columns)
        self.set_location_columns(False)
        filename_page = QWidget()
        filename_layout = QVBoxLayout(filename_page)
        filename_layout.setContentsMargins(0,0,0,0)
        filename_layout.setSpacing(8)
        filename_layout.addWidget(self.filename_tools)
        filename_layout.addWidget(self.filename_table,1)
        self.preview_tabs = QTabWidget()
        self.preview_tabs.addTab(self.tree,tr('当前步骤'))
        self.preview_tabs.addTab(filename_page,tr('文件名实时预览'))
        self.preview_tabs.setAccessibleName(tr('预览视图切换'))
        preview_layout.addWidget(self.preview_tabs,1)
        self.locate_btn = QPushButton(tr('定位选中成员的来源步骤'))
        self.locate_btn.clicked.connect(self.locate_source)
        self.preview_tabs.currentChanged.connect(self.preview_tab_changed)
        preview_layout.addWidget(self.locate_btn)
        self.content.addWidget(preview)
        self.content.setSizes([480,520])
        footer = QHBoxLayout()
        self.footer = footer
        if not production:
            restore = QPushButton(tr('恢复任务 · 待接入'))
            self.resume_btn = restore
            restore.setEnabled(False)
            footer.addWidget(restore)
            self.log_toggle = QPushButton(tr('显示原型日志 ▸'))
            self.log_toggle.setCheckable(True)
            footer.addWidget(self.log_toggle)
            footer.addStretch()
            self.review_btn = QPushButton(tr('查看执行摘要'))
            self.review_btn.setObjectName('Primary')
            footer.addWidget(self.review_btn)
            run = QPushButton(tr('开始执行 · 待接入'))
            self.run_btn = run
            run.setEnabled(False)
            footer.addWidget(run)
        root.addLayout(footer)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        self.log.hide()
        root.addWidget(self.log)
        self.log_toggle.toggled.connect(self.log.setVisible)
        if not production:
            self.log_toggle.toggled.connect(lambda checked:self.log_toggle.setText(tr('隐藏原型日志 ▾') if checked else tr('显示原型日志 ▸')))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.navigation.currentItemChanged.connect(self.select_node)
        if not production:
            self.add_btn.clicked.connect(self.add_layer)
            self.copy_btn.clicked.connect(self.copy_layer)
            self.remove_btn.clicked.connect(self.remove_layer)
        self.mapping_table.itemChanged.connect(self.change)
        self.naming.changed.connect(self.change)
        self.source.textChanged.connect(self.change)
        self.destination.textChanged.connect(self.change)
        self.layer_editor.changed.connect(self.change)
        self.layer_editor.advanced_toggle.toggled.connect(self.change)
        self.layer_editor.show_password.toggled.connect(self.change)
        self.layer_editor.apply_password.connect(self.apply_password)
        self.keep.toggled.connect(self.change)
        self.output_list.toggled.connect(self.change)
        self._loading = False
        self.rebuild_navigation()
        self.apply_theme()
        self.refresh()

        self.transient_scrollbars = TransientScrollBars.attach(self)

    def draft(self):
        return next((draft for draft in self.state.layers if draft.key==self.state.selected),None)

    def save_layer(self):
        draft = self.draft()
        if draft is None or self._loading:
            return
        row = self.layer_editor
        draft.data.update(group=getattr(row,'_group_preference',row.group.value()) if getattr(row,'_single_locked',False) else row.group.value(),
            format=row.format.currentText(),volume=row.volume.isChecked(),size=row.size.text(),
            template=row.template.text(),encrypt=bool(row.password.text()),password=row.password.text(),
            confirm=row.confirm.text(),verify=row.verify.isChecked(),rename=row.rename.isChecked(),
            suffix=row.suffix.text(),expanded=row.advanced_toggle.isChecked(),show_password=row.show_password.isChecked())

    def load_layer(self,draft):
        from sortzip_core.archive_tools import TOOL_FORMATS
        with loading_controls(self,self.layer_editor):
            row,data = self.layer_editor,draft.data
            index = self.state.layers.index(draft)
            row.set_layer_position(index,index==len(self.state.layers)-1)
            row.set_tool_formats(TOOL_FORMATS[getattr(self.state,'archive_tool','bandizip')],data['format'])
            if getattr(row,'_single_locked',False):
                row._group_preference = data['group']
            else:
                row.group.setValue(data['group'])
            for control,key in ((row.volume,'volume'),(row.verify,'verify'),(row.rename,'rename'),
                                (row.show_password,'show_password'),(row.advanced_toggle,'expanded')):
                control.setChecked(data[key])
            for control,key in ((row.size,'size'),(row.template,'template'),(row.password,'password'),(row.confirm,'confirm'),(row.suffix,'suffix')):
                control.setText(data[key])
            row.sync()
            row.apply_password_btn.setVisible(index==0)
            row.apply_password_btn.setEnabled(index==0 and self.password_ready(data) and any(
                FORMATS[d.data['format']]['password'] for d in self.state.layers[1:]))

    @staticmethod
    def password_ready(data):
        return (FORMATS[data['format']]['password'] and len(data['password'])>=8
                and data['password']==data['confirm'])

    def change(self,*args):
        if self._loading:
            return
        self.save_layer()
        self.state.source,self.state.destination = self.source.text(),self.destination.text()
        self.state.mappings = [(self.mapping_table.item(r,0).text() if self.mapping_table.item(r,0) else '',
                               self.mapping_table.item(r,1).text() if self.mapping_table.item(r,1) else '') for r in range(self.mapping_table.rowCount())]
        self.state.rules = self.naming.rules()
        self.state.revision += 1
        self.preview_status.setText(tr('配置已变化，正在更新预览…'))
        self.review_btn.setEnabled(False)
        if self.draft() is not None:
            data = self.draft().data
            self.layer_editor.apply_password_btn.setEnabled(self.draft()==self.state.layers[0] and self.password_ready(data) and
                any(FORMATS[d.data['format']]['password'] for d in self.state.layers[1:]))
        self.timer.start()

    def rebuild_navigation(self):
        self.navigation.blockSignals(True)
        self.navigation.clear()
        nodes = [('mapping',tr('分类映射'),None,False),('naming',tr('原文件命名'),None,False)]
        for index,draft in enumerate(self.state.layers):
            nodes.append((draft.key,tr('第 {0} 层', index+1),index+1,index==len(self.state.layers)-1))
        nodes.append(('final',tr('最终输出'),None,False))
        for key,label,number,outer in nodes:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole,key)
            item.setData(NodeRole.Title,label)
            item.setData(NodeRole.LayerNumber,number)
            item.setData(NodeRole.Outer,outer)
            item.setSizeHint(QSize(176,76))
            self.navigation.addItem(item)
            if key==self.state.selected:
                self.navigation.setCurrentItem(item)
        self.navigation.blockSignals(False)
        self.select_node(self.navigation.currentItem())

    def select_node(self,item,*args):
        if item is None:
            return
        self.save_layer()
        self._preview_tabs[self.state.selected] = self.preview_tabs.currentIndex()
        self.state.selected = item.data(Qt.ItemDataRole.UserRole)
        self.editor_title.setText(item.data(NodeRole.Title))
        self.preview_tabs.setCurrentIndex(self._preview_tabs.get(self.state.selected,1 if self.state.selected=='naming' else 0))
        self.preview_tab_changed(self.preview_tabs.currentIndex())
        draft = self.draft()
        page = {'mapping':self.mapping_page,'naming':self.naming,'final':self.final_page}.get(self.state.selected,self.layer_editor)
        self.pages.setCurrentWidget(page)
        if draft:
            self.load_layer(draft)
        self.copy_btn.setEnabled(draft is not None)
        self.remove_btn.setEnabled(draft is not None and len(self.state.layers)>1)
        self.edit_scroll.verticalScrollBar().setValue(0)
        self.refresh()

    def navigate(self,key):
        for index in range(self.navigation.count()):
            item = self.navigation.item(index)
            if item.data(Qt.ItemDataRole.UserRole)==key:
                self.navigation.setCurrentItem(item)
                return

    def add_layer(self):
        self.save_layer()
        self._append_draft(LayerDraft())

    def _append_draft(self,new):
        outer = self.state.layers[-1].data
        new.data['rename'],new.data['suffix'] = outer['rename'],outer['suffix']
        outer['rename'] = False
        self.state.layers.append(new)
        self.state.selected = new.key
        # Selection has already changed; do not save the previous editor into the new draft.
        with loading_controls(self):
            self.rebuild_navigation()
        self.change()

    def copy_layer(self):
        self.save_layer()
        draft = self.draft()
        if draft:
            self._append_draft(LayerDraft(data=deepcopy(draft.data)))

    def remove_layer(self):
        self.save_layer()
        draft = self.draft()
        if draft is None or len(self.state.layers)==1:
            return
        index = self.state.layers.index(draft)
        suffix = (self.state.layers[-1].data['rename'],self.state.layers[-1].data['suffix'])
        self.state.layers.remove(draft)
        self.state.layers[-1].data['rename'],self.state.layers[-1].data['suffix'] = suffix
        self.state.selected = self.state.layers[min(index,len(self.state.layers)-1)].key
        with loading_controls(self):
            self.rebuild_navigation()
        self.change()

    def add_mapping(self):
        self._loading = True
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)
        self.mapping_table.setItem(row,0,QTableWidgetItem('.pdf'))
        self.mapping_table.setItem(row,1,QTableWidgetItem('文档'))
        self._loading = False
        self.change()

    def remove_mapping(self):
        if self.mapping_table.currentRow()>=0:
            self.mapping_table.removeRow(self.mapping_table.currentRow())
            self.change()

    def apply_password(self):
        self.save_layer()
        source = self.state.layers[0].data
        if not self.password_ready(source):
            return
        targets = [draft.data for draft in self.state.layers[1:] if FORMATS[draft.data['format']]['password']]
        if any(any(data[key] and data[key]!=source['password'] for key in ('password','confirm')) for data in targets):
            if QMessageBox.question(self,tr('覆盖后续层密码'),tr('后续层已有不同密码，是否替换？'),
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:
                return
        for data in targets:
            data.update(password=source['password'],confirm=source['password'],encrypt=True)
        self.change()

    def refresh(self):
        self.timer.stop()
        self.locate_btn.setEnabled(False)
        entries = []
        try:
            entries = self.state.file_entries()
            entries,plan,warnings = self.state.structure()
        except ValueError as error:
            self.tree.clear()
            self._tree_render_key = None
            self.fill_filename_preview(entries)
            self._preview_has_issue = True
            self.preview_status.setText((tr('归档预览不可用，文件名对照仍可查看：') if self.filename_table.rowCount() else tr('文件名预览不可用：'))+str(error))
            self.sync_preview_status()
            self.review_btn.setEnabled(False)
            self._navigation_status(None)
            return
        self.fill_filename_preview(entries,plan)
        self._preview_has_issue = bool(warnings)
        self.review_btn.setEnabled(bool(entries) and not warnings)
        self.preview_status.setText('；'.join(warnings) if warnings else tr('{0} 个原文件 · {1} 层压缩 · 分卷数量执行后确定', len(entries), len(self.state.layers)))
        self.sync_preview_status()
        key = (entries,plan,self.state.selected,getattr(self.state,'compression_enabled',True))
        previous = self._tree_render_key
        if not (previous and previous[0] is entries and previous[1] is plan and previous[2:]==key[2:]):
            self._fill_step_tree(entries,plan)
            self._tree_render_key = key
        elif self.state.selected not in ('mapping','naming'):
            self.locate_btn.setEnabled(self.preview_tabs.currentIndex()==0 and bool(plan.tasks))
        self.summary.setText(tr('输出位置：{0}\n\n{1} 个原文件 → {2} 层压缩 → {3} 个最外层完整单元。\n\n展开右侧产物查看直接输入，选中成员后可定位来源步骤。', self.state.destination, len(entries), len(self.state.layers), len(plan.final_ids)))
        self._navigation_status(plan)

    def _fill_step_tree(self,entries,plan):
        self.tree.clear()
        selected = self.state.selected
        if selected in ('mapping','naming'):
            for entry in entries:
                target = entry['folder']+'/'+(entry.get('classified',entry['source']) if selected=='mapping' else entry['target'])
                QTreeWidgetItem(self.tree,[entry['source'],target])
        elif not getattr(self.state,'compression_enabled',True):
            for entry in entries:
                QTreeWidgetItem(self.tree,[entry['source'],entry['folder']+'/'+entry['target']])
        else:
            index = self.state.layers.index(self.draft()) if self.draft() else len(self.state.layers)-1
            tasks = {task.unit.unit_id:task for task in plan.tasks}
            sources = {'source:'+entry['source']:entry for entry in entries}
            for task in plan.tasks:
                if task.layer_index!=index:
                    continue
                name = task.archive_stem+(task.settings.suffix or '.'+task.settings.archive_format)
                if task.settings.enable_volume:
                    name += tr('（入口卷执行后确定）')
                root = QTreeWidgetItem(self.tree,[tr('{0} 个完整输入单元', len(task.input_ids)),task.unit.folder+'/'+name])
                root.setData(0,Qt.ItemDataRole.UserRole,self.state.layers[index].key if selected=='final' else ('naming' if index==0 else self.state.layers[index-1].key))
                for identity in task.input_ids:
                    previous = tasks.get(identity)
                    label = (previous.archive_stem+'.'+previous.settings.archive_format) if previous else sources[identity]['target']
                    child = QTreeWidgetItem(root,[label,tr('完整单元')+(tr(' · 包含整套分卷') if previous and previous.settings.enable_volume else '')])
                    child.setData(0,Qt.ItemDataRole.UserRole,'naming' if index==0 else self.state.layers[index-1].key)
                root.setExpanded(True)
            self.locate_btn.setEnabled(self.preview_tabs.currentIndex()==0)

    def filename_rows(self,entries,plan=None):
        archives = {}
        if plan is not None:
            final_ids = set(plan.final_ids)
            for task in plan.tasks:
                if task.unit.unit_id in final_ids:
                    name = task.archive_stem+(task.settings.suffix or '.'+task.settings.archive_format)
                    location = task.unit.folder+'/'+name
                    if task.settings.enable_volume:
                        location += tr('（分卷数量执行后确定）')
                    for source in task.unit.original_ids:
                        archives[source] = location
        return ((entry['source'],entry['target'],entry['folder'],archives.get(entry['source'],tr('归档计划待修正')))
            for entry in entries)

    def fill_filename_preview(self,entries,plan=None):
        return render_filename_rows(self,self.filename_rows(entries,plan))

    def filter_filenames(self,*args):
        category = self.category_filter.currentData()
        visible = 0
        for row in range(self.filename_table.rowCount()):
            hidden = category is not None and self.filename_table.item(row,2).text()!=category
            self.filename_table.setRowHidden(row,hidden)
            visible += not hidden
        self.filename_count.setText(tr('{0} / {1} 项', visible, self.filename_table.rowCount()))

    def set_location_columns(self,expanded):
        table = self.filename_table
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if expanded else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for column in (2,3):
            table.setColumnHidden(column,not expanded)
        for column,width in enumerate((150,150,100,180)):
            table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive if expanded else QHeaderView.ResizeMode.Stretch)
            if expanded:
                table.setColumnWidth(column,width)
        table.horizontalScrollBar().setValue(0)

    def preview_tab_changed(self,index):
        self.filename_tools.setVisible(index==1)
        self.sync_preview_status()
        if index==1:
            self.preview_title.setText(tr('文件名实时预览 / 原文件名 → 命名后'))
        else:
            item = self.navigation.currentItem()
            if item:
                self.preview_title.setText(item.data(NodeRole.Title)+tr(' / 输入 → 输出'))
        self.locate_btn.setVisible(index==0)
        self.locate_btn.setEnabled(index==0 and self.state.selected not in ('mapping','naming') and self.tree.topLevelItemCount()>0)

    def sync_preview_status(self):
        self.preview_status.setVisible(self.preview_tabs.currentIndex()==0 or self._preview_has_issue)

    def navigation_issues(self):
        return {}

    def _navigation_status(self,plan):
        issues = self.navigation_issues()
        for index in range(self.navigation.count()):
            item = self.navigation.item(index)
            key,label = item.data(Qt.ItemDataRole.UserRole),item.data(NodeRole.Title)
            draft = next((draft for draft in self.state.layers if draft.key==key),None)
            if draft:
                level = self.state.layers.index(draft)
                count = sum(task.layer_index==level for task in plan.tasks) if plan else '—'
                data = draft.data
                group = data['group'] if FORMATS[data['format']]['multiple'] else 1
                detail = tr('{0} · 每包 {1} {2}', data['format'].upper(), group, tr('文件') if level==0 else tr('单元'))
                issue = tr('密码未确认') if data['password'] and FORMATS[data['format']]['password'] and not self.password_ready(data) else ''
                extra = tr('共 {0} 组', count)
            else:
                detail = {'mapping':tr('{0} 条映射', len(self.state.mappings)),'naming':tr('{0} 条命名规则', len(self.state.rules)),'final':tr('查看外层产物与执行选项')}[key]
                issue,extra = '',''
            issue = issues.get(key,issue)
            if draft and not getattr(self.state,'compression_enabled',True):
                issue = ''
            outer = tr('最外层') if item.data(NodeRole.Outer) else ''
            description = '，'.join(part for part in (label,outer,detail,extra,issue) if part)
            item.setData(NodeRole.Summary,detail)
            item.setData(NodeRole.Error,issue)
            item.setText(label+'\n'+detail)
            item.setData(Qt.ItemDataRole.AccessibleTextRole,description)
            item.setToolTip(description)

    def locate_source(self):
        item = self.tree.currentItem()
        if item and item.data(0,Qt.ItemDataRole.UserRole):
            self.navigate(item.data(0,Qt.ItemDataRole.UserRole))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'paths'):
            self.layout_paths()
        if hasattr(self,'content'):
            orientation = Qt.Orientation.Vertical if self.width()<WORKBENCH_WIDE_WIDTH else Qt.Orientation.Horizontal
            if self.content.orientation()!=orientation:
                self.content.setOrientation(orientation)
                self.content.setSizes([400,600] if orientation==Qt.Orientation.Vertical else [480,520])

    def layout_paths(self):
        wide = self.width()>=WORKBENCH_WIDE_WIDTH
        if wide==self._paths_wide:
            return
        self._paths_wide = wide
        for widget in (self.source_label,self.source,self.destination_label,self.destination):
            self.paths.removeWidget(widget)
        self.paths.setColumnStretch(1,1)
        self.paths.setColumnStretch(3,1 if wide else 0)
        self.paths.addWidget(self.source_label,0,0)
        self.paths.addWidget(self.source,0,1)
        self.paths.addWidget(self.destination_label,0 if wide else 1,2 if wide else 0)
        self.paths.addWidget(self.destination,0 if wide else 1,3 if wide else 1)

    def toggle_theme(self):
        self._dark = not self._dark
        self.theme_btn.setText(tr('浅色模式') if self._dark else tr('深色模式'))
        self.apply_theme()

    def apply_theme(self):
        bg,surface,text,border,muted,accent = ('#171d25','#222c38','#edf2f7','#3c4a5d','#a7b6c8','#8bc5ff') if self._dark else ('#f4f6fa','#ffffff','#203044','#d9e1eb','#60728a','#0865be')
        palette = QPalette(self.palette())
        for role,color in ((QPalette.Window,bg),(QPalette.Base,surface),(QPalette.AlternateBase,bg),
                           (QPalette.Text,text),(QPalette.WindowText,text),(QPalette.ButtonText,text),
                           (QPalette.Button,surface),(QPalette.Highlight,accent),(QPalette.HighlightedText,'#ffffff')):
            palette.setColor(role,QColor(color))
        self.setPalette(palette)
        selection = '#2a4059' if self._dark else '#e7f1ff'
        for view in (self.tree,self.filename_table):
            preview_palette = QPalette(palette)
            for group in (QPalette.ColorGroup.Active,QPalette.ColorGroup.Inactive,QPalette.ColorGroup.Disabled):
                preview_palette.setColor(group,QPalette.ColorRole.Highlight,QColor(selection))
                preview_palette.setColor(group,QPalette.ColorRole.HighlightedText,QColor(text))
            view.setPalette(preview_palette)
        self.setStyleSheet(f'''
            QWidget {{font-family:"Microsoft YaHei UI","Segoe UI Symbol";font-size:13px;color:{text};}}
            WorkbenchPrototype {{background:{bg};}}
            QLabel#Brand {{font-size:19px;font-weight:600;}}
            QLabel#SectionTitle {{font-size:16px;font-weight:600;padding:8px 0;}}
            QLabel#Hint {{color:{muted};}}
            QLabel#Badge {{color:{accent};padding:4px 8px;border:1px solid {border};border-radius:5px;}}
            QScrollArea,QStackedWidget,QListWidget,QTreeWidget,QTableWidget,QTextEdit {{background:{surface};alternate-background-color:{bg};border:1px solid {border};border-radius:6px;}}
            QTreeWidget#StepPreview,QTableWidget#FilenamePreview {{outline:0;selection-background-color:{selection};selection-color:{text};}}
            QTreeWidget#StepPreview::item:selected,QTableWidget#FilenamePreview::item:selected {{background:{selection};color:{text};border:0;}}
            QScrollArea QWidget,QStackedWidget QWidget {{background:{surface};}}
            QListWidget::item {{padding:8px;border-bottom:1px solid {border};}}
            QListWidget::item:selected {{background:{'#243e59' if self._dark else '#e7f1ff'};color:{accent};border-left:3px solid {accent};}}
            QListWidget {{outline:0;}}
            QTabWidget::pane {{border:0;}}
            QTabBar::tab {{padding:8px 12px;background:{surface};border-bottom:2px solid {border};}}
            QTabBar::tab:selected {{color:{accent};border-bottom:2px solid {accent};}}
            QLineEdit,QSpinBox,QComboBox {{background:{surface};border:1px solid {border};border-radius:4px;padding:5px;min-height:20px;}}
            QLineEdit:focus,QSpinBox:focus {{border:1px solid {accent};}}
            QuietFocusComboBox {{border:1px solid {border};outline:0;}}
            QuietFocusComboBox:focus {{border:1px solid {border};}}
            QuietFocusComboBox[keyboardFocus="true"] {{background:{'#2a3b50' if self._dark else '#edf4fc'};border:1px solid {border};}}
            QuietFocusComboBox:disabled {{background:{bg};color:{muted};border:1px solid {border};}}
            QSpinBox QLineEdit {{border:0;padding:0;min-height:0;}}
            QSpinBox QToolButton {{border:0;border-left:1px solid {border};background:transparent;font-size:14px;}}
            QPushButton {{padding:7px 10px;background:{surface};border:1px solid {border};border-radius:5px;}}
            QPushButton:hover,QToolButton:hover {{background:{'#243e59' if self._dark else '#e7f1ff'};}}
            QPushButton:pressed,QToolButton:pressed {{background:{'#31516f' if self._dark else '#d4e7fc'};}}
            QPushButton#Primary {{background:#096bc5;color:white;border:0;}}
            QPushButton#Primary:hover {{background:#075eaf;}}
            QPushButton#Primary:pressed {{background:#064d91;}}
            QPushButton:disabled,QLineEdit:disabled,QToolButton:disabled {{color:{muted};}}
            QGroupBox#LayerCard {{border:0;}}
            QLabel#LayerBadge {{color:{accent};font-size:10px;padding:2px 5px;}}
            QHeaderView::section {{background:{bg};padding:6px;border:0;border-bottom:1px solid {border};}}
            QSplitter::handle {{background:{border};}}
            QScrollBar:horizontal {{height:10px;margin:2px;border:0;background:{bg};}}
            QScrollBar::handle:horizontal {{background:{'#52667e' if self._dark else '#bac7d7'};min-width:32px;border-radius:3px;}}
            QScrollBar::handle:horizontal:hover {{background:{'#7190af' if self._dark else '#8da6c2'};}}
            QScrollBar::handle:horizontal:pressed {{background:{'#8aa9c9' if self._dark else '#6b8dad'};}}
            QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal {{width:0;height:0;border:0;background:transparent;}}
            QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal {{background:transparent;border:0;}}
            QScrollBar:vertical {{width:10px;margin:2px;border:0;background:{bg};}}
            QScrollBar::handle:vertical {{background:{'#52667e' if self._dark else '#bac7d7'};min-height:32px;border-radius:3px;}}
            QScrollBar::handle:vertical:hover {{background:{'#7190af' if self._dark else '#8da6c2'};}}
            QScrollBar::handle:vertical:pressed {{background:{'#8aa9c9' if self._dark else '#6b8dad'};}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;width:0;border:0;background:transparent;}}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;border:0;}}
            QAbstractScrollArea::corner {{background:{surface};border:0;}}
        ''')
        self.naming.fit_table()
