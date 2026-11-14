"""Production-only presentation and shared commands for the flow workbench."""
from PySide6.QtCore import Qt,QSize,QPointF
from PySide6.QtGui import QAction,QIcon,QPixmap,QPainter,QPen,QColor,QPainterPath
from PySide6.QtWidgets import QWidget,QHBoxLayout,QToolButton,QMenu,QPushButton,QLineEdit,QProgressBar
from sortzip_core import __version__
from sortzip_core.widgets import show_manual_dialog,DropResumeButton
from sortzip_core.i18n import tr


def line_icon(name,dark=False):
    pixmap = QPixmap(32,32)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#edf2f7' if dark else '#303842'),1.4,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
    if name=='plus':
        painter.drawLine(3,8,13,8); painter.drawLine(8,3,8,13)
    elif name=='folder':
        path = QPainterPath(); path.moveTo(2,4); path.lineTo(6,4); path.lineTo(8,6); path.lineTo(14,6); path.lineTo(14,13); path.lineTo(2,13); path.closeSubpath(); painter.drawPath(path)
    elif name=='refresh':
        painter.drawArc(3,3,10,10,40*16,290*16); painter.drawLine(13,2,13,6); painter.drawLine(9,6,13,6)
    elif name=='logs':
        painter.drawRoundedRect(2,2,12,12,2,2)
        for y in (5,8,11): painter.drawLine(5,y,11,y)
    elif name=='copy':
        painter.drawRoundedRect(5,5,9,9,1,1); painter.drawLine(2,11,2,2); painter.drawLine(2,2,11,2)
    elif name=='delete':
        painter.drawLine(3,4,13,4); painter.drawLine(6,2,10,2); painter.drawLine(4,6,5,14); painter.drawLine(5,14,11,14); painter.drawLine(11,14,12,6)
    else:
        for x in (3,8,13): painter.drawEllipse(QPointF(x,8),.8,.8)
    painter.end()
    return QIcon(pixmap)


class WorkbenchPolishMixin:
    def _command(self,key,text,callback,icon=None,checkable=False):
        action = QAction(text,self)
        action.setToolTip(text)
        action.setCheckable(checkable)
        if callback:
            action.triggered.connect(callback)
        self.commands[key] = action
        if icon:
            action.setProperty('lineIcon',icon)
        return action

    def _tool(self,action,parent=None,text=False):
        button = QToolButton(parent)
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text else Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setAccessibleName(action.text())
        button.setFixedHeight(32)
        if not text: button.setFixedWidth(32)
        return button

    def _more(self,title,actions,parent):
        button = QToolButton(parent)
        button.setAccessibleName(title)
        button.setToolTip(title)
        button.setProperty('lineIcon','more')
        button.setFixedSize(32,32)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        for action in actions: menu.addAction(action)
        button.setMenu(menu)
        self.icon_buttons.append(button)
        return button

    def _context_menu(self,widget,actions):
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def show(point):
            if widget is self.navigation:
                item = widget.itemAt(point)
                if item is not None: widget.setCurrentItem(item)
            self._sync_action_states()
            menu = QMenu(widget)
            menu.addActions(actions)
            menu.exec(widget.viewport().mapToGlobal(point))
            menu.deleteLater()
        widget.customContextMenuRequested.connect(show)

    def _init_commands(self):
        self.commands,self.icon_buttons = {},[]
        self._polished = False
        for key,attr,text,callback,icon in (
            ('theme','theme_btn',tr('深色模式'),self.toggle_theme,None),
            ('settings','settings_btn',tr('设置'),self.show_settings,None),
            ('add_layer','add_btn',tr('添加压缩层'),self.add_layer,'plus'),
            ('copy_layer','copy_btn',tr('复制本层'),self.copy_layer,'copy'),
            ('remove_layer','remove_btn',tr('删除本层'),self.remove_layer,'delete'),
            ('summary','review_btn',tr('参数摘要'),self.review_summary,None)):
            setattr(self,attr,self._command(key,text,callback,icon))
        self.log_toggle = self._command('logs',tr('日志'),None,'logs',checkable=True)

    def _polish_workbench(self):
        self._command('manual',tr('使用手册'),lambda:show_manual_dialog(self))
        self._command('github',tr('GitHub 项目主页'),self._open_project)
        header = self.layout().itemAt(0).layout()
        self.findChild(QWidget,'Brand').setText('SortZip')
        global_button = self._more(tr('全局菜单'),[],self)
        appearance = global_button.menu().addMenu(tr('外观'))
        appearance.addAction(self.theme_btn)
        for key in ('settings','manual','github'): global_button.menu().addAction(self.commands[key])
        global_button.menu().addSeparator()
        global_button.menu().addAction(__version__).setEnabled(False)
        header.addWidget(global_button)
        self.global_menu_btn = global_button

        for name in ('source','destination'):
            container = getattr(self,name+'_container')
            layout = container.layout()
            field = getattr(self,name)
            action = self._command('browse_'+name,tr('选择输入目录') if name=='source' else tr('选择输出目录'),lambda checked=False,field=field:self._browse_folder(field),'folder')
            field.addAction(action,QLineEdit.ActionPosition.TrailingPosition)
            for tool in field.findChildren(QToolButton):
                tool.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                tool.setAccessibleName(action.text())

        heading = QWidget()
        heading.setFixedSize(180,32)
        row = QHBoxLayout(heading); row.setContentsMargins(0,0,0,0); row.setSpacing(4)
        self.navigation_heading.setFixedWidth(96)
        # Locate the shared section row by its existing caption, independent of root indexes.
        for index in range(self.layout().count()):
            candidate = self.layout().itemAt(index).layout()
            if candidate is not None and candidate.indexOf(self.navigation_heading)>=0:
                candidate.replaceWidget(self.navigation_heading,heading); break
        row.addWidget(self.navigation_heading); row.addStretch()
        row.addWidget(self._tool(self.add_btn,heading))
        self.layer_menu_btn = self._more(tr('当前层操作'),[self.copy_btn,self.remove_btn],heading)
        row.addWidget(self.layer_menu_btn)
        self._context_menu(self.navigation,[self.copy_btn,self.remove_btn])

        self._polish_editors()
        self._polish_footer()
        self.locate_btn.setObjectName('QuietAction')
        self.locate_btn.setText(tr('定位来源步骤'))
        self._polished = True
        self.apply_theme()
        self._sync_action_states()

    def _polish_editors(self):
        layout = self.mapping_page.layout()
        self.map_all_btn = self._command('map_all',tr('映射全部'),self._map_all)
        self.map_clear_btn = self._command('map_clear',tr('清空映射'),self._clear_mappings)
        add = self._command('map_add',tr('添加类型'),self.add_mapping,'plus')
        remove = self._command('map_remove',tr('删除选中类型'),self.remove_mapping,'delete')
        common = self._command('common',tr('常用类型'),None,checkable=True)
        common.toggled.connect(self.preset_panel.setVisible)
        self.preset_grid.addWidget(self.preset_category,0,0,1,4)
        self.mapping_toolbar = QHBoxLayout(); self.mapping_toolbar.setSpacing(8)
        self.mapping_toolbar.addWidget(self._tool(add,text=True)); self.mapping_toolbar.addWidget(self._tool(common,text=True))
        self.mapping_toolbar.addStretch()
        self.mapping_toolbar.addWidget(self._more(tr('映射操作'),[self.map_all_btn,remove,self.map_clear_btn],self.mapping_page))
        layout.insertLayout(1,self.mapping_toolbar)
        self._context_menu(self.mapping_table,[self.map_all_btn,remove,self.map_clear_btn])
        self.mapping_table.currentCellChanged.connect(self._sync_action_states)
        controls = QHBoxLayout(); controls.setSpacing(8)
        add_rule = self._command('rule_add',tr('添加规则'),lambda:self.naming.add_rule('*','{original}{ext}'),'plus')
        remove_rule = self._command('rule_remove',tr('删除选中规则'),self.naming.remove_rule,'delete')
        clear_rules = self._command('rule_clear',tr('清空命名规则'),self.naming.clear_rules)
        controls.addWidget(self._tool(add_rule,text=True)); controls.addStretch()
        controls.addWidget(self._more(tr('命名规则操作'),[remove_rule,clear_rules],self.naming))
        self.naming.layout().insertLayout(3,controls)
        self._context_menu(self.naming.table,[remove_rule,clear_rules])
        self.naming.table.currentCellChanged.connect(self._sync_action_states)

    def _polish_footer(self):
        restore = self._command('resume',tr('恢复任务'),self._import_resume)
        self.resume_btn = DropResumeButton(tr('任务'),self)
        self.resume_btn.resumeDropped.connect(self._start_resume)
        self.resume_btn.setObjectName('QuietAction')
        self.resume_btn.setAccessibleName(tr('任务菜单，可拖入断点恢复'))
        menu = QMenu(self.resume_btn); menu.addActions([restore,self.review_btn]); self.resume_btn.setMenu(menu)
        while self.footer.count(): self.footer.takeAt(0)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0,100)
        self.progress_bar.setValue(0)
        self.run_btn = QPushButton(tr('开始执行'),self)
        self.run_btn.setObjectName("Primary")
        self.cancel_btn = QPushButton(tr('取消'),self)
        self.cancel_btn.setEnabled(False)
        self.footer.setSpacing(8)
        self.footer.addWidget(self.resume_btn)
        self.footer.addWidget(self._tool(self.log_toggle,text=True))
        self.footer.addWidget(self.progress_bar,1)
        self.progress_bar.setVisible(False)
        policy = self.progress_bar.sizePolicy(); policy.setRetainSizeWhenHidden(True); self.progress_bar.setSizePolicy(policy)
        self.footer.addWidget(self.cancel_btn)
        self.footer.addWidget(self.run_btn)
        for key,button,callback in (('run',self.run_btn,self._run),('cancel',self.cancel_btn,self._cancel)):
            action = self._command(key,button.text(),callback)
            button.clicked.connect(action.trigger)
        self.cancel_btn.setVisible(False)
        self.run_btn.setMinimumHeight(32)

    def _sync_action_states(self,*args):
        if not getattr(self,'_polished',False): return
        busy = self._task_busy()
        layer = self.draft() is not None and self.state.compression_enabled
        self.commands['add_layer'].setEnabled(not busy and self.state.compression_enabled)
        self.copy_btn.setEnabled(not busy and layer)
        self.remove_btn.setEnabled(not busy and layer and len(self.state.layers)>1)
        self.layer_menu_btn.setEnabled(not busy and layer)
        for key in ('settings','resume','refresh','browse_source','browse_destination','map_add','map_all','rule_add','common'):
            self.commands[key].setEnabled(not busy)
        self.map_clear_btn.setEnabled(not busy and self.mapping_table.rowCount()>0)
        self.commands['map_remove'].setEnabled(not busy and self.mapping_table.currentRow()>=0)
        self.commands['rule_remove'].setEnabled(not busy and self.naming.table.currentRow()>=0)
        self.commands['rule_clear'].setEnabled(not busy and self.naming.table.rowCount()>0)
        self.commands['run'].setEnabled(not busy)
        self.commands['cancel'].setEnabled(busy and self.cancel_btn.isEnabled())
        self.cancel_btn.setVisible(busy)
        self.progress_bar.setVisible(busy or self.progress_bar.value()>0)
        if busy: self.review_btn.setEnabled(False)
        if hasattr(self,'structure_history') and 'undo' in self.commands:
            self.commands['undo'].setEnabled(not busy and self.structure_history.stack.canUndo())
            self.commands['redo'].setEnabled(not busy and self.structure_history.stack.canRedo())

    def _open_project(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl('https://github.com/WindySumm/SortZip'))

    def apply_theme(self):
        super().apply_theme()
        if not getattr(self,'_polished',False): return
        dark = self._dark
        bg,surface,text,border = ('#171d25','#222c38','#edf2f7','#3c4a5d') if dark else ('#f7f7f8','#ffffff','#202124','#e2e5e9')
        self.setStyleSheet(self.styleSheet()+f'''
            WorkbenchWindow {{background:{bg};}}
            QDialog {{background:{surface};color:{text};}}
            QLabel#Brand {{font-size:19px;font-weight:600;}}
            QLabel#SectionTitle {{font-size:15px;font-weight:600;padding:8px 0;}}
            QScrollArea,QStackedWidget,QListWidget {{border:0;background:{surface};}}
            QTableWidget,QTreeWidget,QTextEdit {{border:1px solid {border};border-radius:5px;}}
            QToolButton,QPushButton#QuietAction {{background:transparent;border:0;border-radius:5px;padding:4px 6px;}}
            QToolButton:hover,QPushButton#QuietAction:hover {{background:{'#2a4059' if dark else '#e9edf2'};}}
            QToolButton:checked {{background:{'#2a4059' if dark else '#e7f1ff'};}}
            QToolButton::menu-indicator {{image:none;width:0;height:0;}}
            QToolButton:disabled {{color:{'#78889a' if dark else '#9aa2ac'};}}
            QMenu {{background:{surface};color:{text};border:1px solid {border};padding:5px;}}
            QMenu::item {{padding:7px 24px 7px 10px;border-radius:3px;}}
            QMenu::item:selected {{background:{'#2a4059' if dark else '#e7f1ff'};}}
            QMenu::item:disabled {{color:{'#78889a' if dark else '#9aa2ac'};}}
            QProgressBar {{border:0;background:{bg};text-align:center;max-height:18px;}}
            QProgressBar::chunk {{background:{'#39668d' if dark else '#c4ddf6'};border-radius:3px;}}
        ''')
        self.edit_scroll.widget().layout().setContentsMargins(16,0,16,0)
        for action in self.commands.values():
            if action.property('lineIcon'): action.setIcon(line_icon(action.property('lineIcon'),dark))
        for button in self.icon_buttons: button.setIcon(line_icon('more',dark))
        if hasattr(self,'native_caption'):
            self.native_caption.set_theme(dark,bg,text)
