"""Native editor for independently configured compression layers."""
from dataclasses import asdict
from PySide6.QtCore import Signal,QSignalBlocker

from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QComboBox, QLineEdit, QPushButton, QScrollArea, QLabel, QMessageBox)

from sortzip_core.formats import FORMATS, LAYER_FORMATS
from sortzip_core.nesting import Layer, plan_layers
from sortzip_core.widgets import show_styled_dialog
from sortzip_core.ui_controls import StateCheckBox, CompactFieldRow, StepSpinBox, PasswordVisibility
from PySide6.QtGui import QFont
from sortzip_core.i18n import tr


class LayerRow(QGroupBox):
    changed = Signal()
    apply_password = Signal()
    def __init__(self, value, parent=None, *, format_combo_factory=QComboBox,actions=True,inline_template=True):
        super().__init__(parent)
        self.setObjectName('LayerCard')
        form = QFormLayout(self)
        form.setContentsMargins(16,16,16,16)
        form.setVerticalSpacing(12)
        self.layer_title = QLabel()
        self.layer_title.setObjectName('LayerTitle')
        title_font = self.layer_title.font()
        title_font.setPointSizeF(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.layer_title.setFont(title_font)
        self.outer_badge = QLabel(tr('最外层'))
        self.outer_badge.setObjectName('LayerBadge')
        self.header = QWidget()
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0,0,0,0)
        header_layout.setSpacing(8)
        header_layout.addWidget(self.layer_title)
        header_layout.addWidget(self.outer_badge)
        header_layout.addStretch()
        form.addRow(self.header)
        self.group = StepSpinBox()
        self.group.setRange(1,9999)
        self.group.setValue(value.group_size)
        self.format = format_combo_factory()
        self.tool_formats = FORMATS
        self.format.addItems(FORMATS if value.archive_format in FORMATS else LAYER_FORMATS)
        self.format.setCurrentText(value.archive_format)
        self.volume = StateCheckBox(tr('启用分卷'))
        self.volume.setChecked(value.enable_volume)
        self.size = QLineEdit(value.volume or '')
        self.size.setPlaceholderText(tr('留空自动；如 100MB'))
        self.template = QLineEdit(value.name_template)
        self.template.setToolTip(tr('{original}：本层输入单元序号范围；{source_range}：原文件序号范围；{n}：本层组序号；{folder}：目录名'))
        self.password = QLineEdit(value.password)
        self.confirm = QLineEdit(value.password)
        self.password.setPlaceholderText(tr('密码留空即为不启用本层密码'))
        self.confirm.setPlaceholderText(tr('再次输入密码'))
        for edit in (self.password,self.confirm):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password = PasswordVisibility((self.password,self.confirm),self)
        self.verify = StateCheckBox(tr('压缩后校验完整性'))
        self.verify.setChecked(value.verify)
        self.rename = StateCheckBox(tr('修改最外层后缀'))
        self.rename.setChecked(value.suffix is not None)
        self.suffix = QLineEdit(value.suffix or '.'+value.archive_format)
        self.group_label = QLabel(tr('每包单元数'))
        self.format_label = QLabel(tr('压缩格式'))
        self.basic_fields = CompactFieldRow(self.group_label,self.group,self.format_label,self.format)
        form.addRow(self.basic_fields)
        volume_row = QHBoxLayout()
        volume_row.addWidget(self.volume)
        volume_row.addWidget(self.size)
        form.addRow(volume_row)
        form.addRow(self.verify)
        self.advanced_toggle = QPushButton(tr('命名与密码 ▸'))
        self.advanced_toggle.setCheckable(True)
        self.advanced = QWidget()
        self.advanced.setObjectName('LayerAdvanced')
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0,0,0,0)
        for label,widget in [(tr('归档名称模板'),self.template),(tr('密码'),self.password),
                (tr('确认密码'),self.confirm)]:
            if inline_template or widget is not self.template:
                advanced_form.addRow(label,widget)
        self.apply_password_btn = QPushButton(tr('将密码应用到后续层'))
        self.apply_password_btn.clicked.connect(self.apply_password)
        self.apply_password_btn.hide()
        advanced_form.addRow(self.apply_password_btn)
        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda checked:self.advanced_toggle.setText(tr('命名与密码 ▾') if checked else tr('命名与密码 ▸')))
        form.addRow(self.advanced_toggle)
        form.addRow(self.advanced)
        suffix_row = QHBoxLayout()
        suffix_row.addWidget(self.rename)
        suffix_row.addWidget(self.suffix)
        form.addRow(suffix_row)
        if actions:
            self.copy_btn = QPushButton(tr('复制本层'))
            self.remove_btn = QPushButton(tr('删除本层'))
            row = QHBoxLayout()
            row.addWidget(self.copy_btn)
            row.addWidget(self.remove_btn)
            form.addRow(row)
        self.outer = False
        self.format.currentTextChanged.connect(self.sync)
        self.volume.toggled.connect(self.sync)
        self.rename.toggled.connect(self.sync)
        self.password.textChanged.connect(self.sync)
        for edit in (self.size,self.template,self.password,self.confirm,self.suffix):
            edit.textChanged.connect(self.changed)
        self.group.valueChanged.connect(self.changed)
        self.format.currentTextChanged.connect(self.changed)
        for check in (self.volume,self.rename,self.verify):
            check.toggled.connect(self.changed)
        self.sync()

    def set_layer_position(self,index,outer):
        self.outer = outer
        title = tr('第 {0} 层', index+1)
        self.setTitle('')
        self.setAccessibleName(title+(tr('，最外层') if outer else ''))
        self.layer_title.setText(title)
        self.outer_badge.setVisible(outer)
        self.group_label.setText(tr('每包文件数') if index==0 else tr('每包单元数'))
        self.basic_fields.refresh_metrics()
        self.sync()

    def sync(self, *args):
        capability = LAYER_FORMATS[self.format.currentText()]
        self.password.setEnabled(capability['password'])
        self.confirm.setEnabled(capability['password'])
        self.show_password.setEnabled(capability['password'])
        tool_volume = self.tool_formats.get(self.format.currentText(),{}).get('volume',False)
        self.volume.setEnabled(capability['volume'] and (tool_volume or self.volume.isChecked()))
        self.size.setEnabled(tool_volume and self.volume.isChecked())
        # Retain values while disabled, so returning to ZIP/7Z restores preferences.
        if not capability['multiple'] and not getattr(self,'_single_locked',False):
            self._group_preference = self.group.value()
            self._single_locked = True
            self.group.setValue(1)
        elif capability['multiple'] and getattr(self,'_single_locked',False):
            self._single_locked = False
            self.group.setValue(self._group_preference)
        self.group.setEnabled(capability['multiple'])
        self.group.setToolTip('' if capability['multiple'] else tr('每包固定为 1 单元，且该单元只能有 1 个实体文件'))
        self.rename.setEnabled(self.outer)
        self.rename.setToolTip('' if self.outer else tr('中间层保留真实后缀和分卷结构'))
        self.suffix.setEnabled(self.outer and self.rename.isChecked())

    def set_tool_formats(self,formats,current):
        self.tool_formats = formats
        choices = list(formats)
        if current not in formats:
            choices.append(current)
        with QSignalBlocker(self.format):
            self.format.clear()
            self.format.addItems(choices)
            self.format.setCurrentText(current)
            if current not in formats:
                self.format.model().item(self.format.currentIndex()).setEnabled(False)

    def value(self, validate_confirmation=True):
        capability = LAYER_FORMATS[self.format.currentText()]
        encrypted = capability['password'] and bool(self.password.text())
        password = self.password.text() if encrypted else ''
        if validate_confirmation and encrypted and len(password)<8:
            raise ValueError(tr('密码至少需要 8 位；清空密码表示本层不加密'))
        if validate_confirmation and encrypted and password!=self.confirm.text():
            raise ValueError(tr('两次输入的压缩密码不一致'))
        return Layer(self.group.value() if capability['multiple'] else 1,
            self.format.currentText(), self.volume.isChecked() and capability['volume'],
            self.size.text().strip() or None, self.template.text(),
            self.suffix.text().strip() if self.outer and self.rename.isChecked() else None,
            password,self.verify.isChecked())


class LayerStack(QWidget):
    """Inline card list used by the unified compression page."""
    changed = Signal()

    def __init__(self,layers,parent=None,required=()):
        super().__init__(parent)
        self.setObjectName('LayerStack')
        self.rows = []
        self._updating = False
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(4,4,4,4)
        self.layout.setSpacing(14)
        self.add_btn = QPushButton(tr('＋ 添加一层压缩'))
        self.add_btn.setObjectName('AddLayerButton')
        self.add_btn.clicked.connect(lambda:self.add_layer(Layer()))
        self.layout.addWidget(self.add_btn)
        self.layout.addStretch()
        self.set_layers(layers,required)

    def set_layers(self,layers,required=()):
        self._updating = True
        for row in self.rows:
            self.layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.rows = []
        for value in layers or (Layer(),):
            self._insert(value)
        self.sync()
        self._updating = False
        self.changed.emit()

    def _insert(self,value):
        row = LayerRow(value,self)
        self.rows.append(row)
        self.layout.insertWidget(self.layout.indexOf(self.add_btn),row)
        row.copy_btn.clicked.connect(lambda:self.copy_layer(row))
        row.remove_btn.clicked.connect(lambda:self.remove_layer(row))
        row.apply_password.connect(self.apply_first_password)
        row.changed.connect(self._notify)

    def _notify(self):
        if not self._updating:
            self._sync_password_action()
            self.changed.emit()

    def _sync_password_action(self):
        source = self.rows[0]
        targets = [row for row in self.rows[1:] if FORMATS[row.format.currentText()]['password']]
        for index,row in enumerate(self.rows):
            row.apply_password_btn.setVisible(index==0)
        valid = (FORMATS[source.format.currentText()]['password']
                 and len(source.password.text())>=8 and source.password.text()==source.confirm.text())
        source.apply_password_btn.setEnabled(bool(valid and targets))
        source.apply_password_btn.setToolTip(tr('应用到当前已有的 ZIP/7Z 后续层，自动启用密码；跳过不支持密码的格式。')
            if valid and targets else tr('请先输入第一层两次一致的至少 8 位密码，且添加支持密码的后续层。'))

    def apply_first_password(self):
        self._sync_password_action()
        source = self.rows[0]
        if not source.apply_password_btn.isEnabled():
            return
        password = source.password.text()
        targets = [row for row in self.rows[1:] if FORMATS[row.format.currentText()]['password']]
        conflicts = [str(self.rows.index(row)+1) for row in targets
                     if any(text and text!=password for text in (row.password.text(),row.confirm.text()))]
        if conflicts and QMessageBox.question(self,tr('覆盖后续层密码'),
                tr('第 {0} 层已填写不同密码，是否替换为第一层密码？', ', '.join(conflicts)),
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:
            return
        self._updating = True
        try:
            for row in targets:
                row.password.setText(password)
                row.confirm.setText(password)
                row.sync()
        finally:
            self._updating = False
        self._notify()

    def _outer_suffix(self):
        row = self.rows[-1]
        return row.rename.isChecked(),row.suffix.text()

    def add_layer(self,value):
        suffix = self._outer_suffix()
        self._updating = True
        self._insert(value)
        self.sync(suffix)
        self._updating = False
        self.changed.emit()

    def copy_layer(self,row):
        try:
            self.add_layer(row.value())
        except ValueError as error:
            show_styled_dialog(self,tr('层参数无效'),str(error))

    def remove_layer(self,row):
        if len(self.rows)<=1:
            return
        suffix = self._outer_suffix()
        self._updating = True
        self.rows.remove(row)
        self.layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self.sync(suffix)
        self._updating = False
        self.changed.emit()

    def sync(self,suffix=None):
        for index,row in enumerate(self.rows):
            row.set_layer_position(index,index==len(self.rows)-1)
            row.group.setPrefix('')
            row.group.setToolTip(tr('每包原始文件数') if index==0 else tr('每包上一层完整单元数，分卷不会拆散'))
            row.remove_btn.setEnabled(len(self.rows)>1)
            if not row.outer:
                row.rename.setChecked(False)
            row.sync()
        if suffix is not None:
            self.rows[-1].suffix.setText(suffix[1])
            self.rows[-1].rename.setChecked(suffix[0])
        self._sync_password_action()

    def values(self,validate_confirmation=True):
        return tuple(row.value(validate_confirmation) for row in self.rows)

    def password_flags(self):
        return [bool(FORMATS[row.format.currentText()]['password'] and row.password.text()) for row in self.rows]

    def preferences(self):
        return [dict(group=getattr(row,'_group_preference',row.group.value())
                     if getattr(row,'_single_locked',False) else row.group.value(),
                     volume=row.volume.isChecked(),encrypted=bool(row.password.text())) for row in self.rows]

    def restore_preferences(self,values):
        if not isinstance(values,list):
            return
        self._updating = True
        for row,value in zip(self.rows,values):
            if not isinstance(value,dict):
                continue
            group = value.get('group')
            if type(group) is int and 1<=group<=9999:
                if getattr(row,'_single_locked',False):
                    row._group_preference = group
                else:
                    row.group.setValue(group)
            for key,check in [('volume',row.volume)]:
                if type(value.get(key)) is bool:
                    check.setChecked(value[key])
        self._updating = False
        self.changed.emit()


class LayerEditor(QDialog):
    def __init__(self, layers, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('多层压缩配置'))
        self.resize(580,660)
        layout = QVBoxLayout(self)
        label = QLabel(tr('每层按上一层完整单元分组，整套分卷始终一起处理。\n仅最外层可修改后缀；各层密码只保留在本次窗口会话中。'))
        label.setWordWrap(True)
        layout.addWidget(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.rows_layout = QVBoxLayout(container)
        scroll.setWidget(container)
        layout.addWidget(scroll)
        self.rows = []
        self.layers = tuple(layers)
        for layer in layers:
            self.add_layer(layer)
        controls = QHBoxLayout()
        add = QPushButton(tr('添加一层'))
        add.clicked.connect(lambda:self.add_layer(Layer()))
        controls.addWidget(add)
        controls.addStretch()
        cancel = QPushButton(tr('取消'))
        cancel.clicked.connect(self.reject)
        save = QPushButton(tr('应用'))
        save.clicked.connect(self.save)
        controls.addWidget(cancel)
        controls.addWidget(save)
        layout.addLayout(controls)

    def add_layer(self, value):
        row = LayerRow(value,self)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        row.copy_btn.clicked.connect(lambda:self.copy_layer(row))
        row.remove_btn.clicked.connect(lambda:self.remove_layer(row))
        self.sync()

    def copy_layer(self, row):
        try:
            value = row.value()
        except ValueError as error:
            show_styled_dialog(self,tr('层参数无效'),str(error))
            return
        self.add_layer(value)

    def remove_layer(self, row):
        if len(self.rows)<=1:
            return
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self.sync()

    def sync(self):
        for index,row in enumerate(self.rows):
            row.set_layer_position(index,index==len(self.rows)-1)
            row.remove_btn.setEnabled(len(self.rows)>1)
            row.sync()

    def save(self):
        try:
            values = tuple(row.value() for row in self.rows)
            plan_layers((),values)
        except ValueError as error:
            show_styled_dialog(self,tr('层参数无效'),str(error))
            return
        self.layers = values
        self.accept()


def public_layers(layers):
    """Settings contain preferences only; all password material stays in memory."""
    return [{key:value for key,value in asdict(layer).items() if key!='password'} for layer in layers]
