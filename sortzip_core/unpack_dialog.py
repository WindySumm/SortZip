"""Collect local extraction choices and an ordered, task-only password list."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,
                               QLabel,QLineEdit,QPushButton,QScrollArea,QVBoxLayout,QWidget)

from sortzip_core import delivery_manifest
from sortzip_core.i18n import tr
from sortzip_core.ui_controls import StateCheckBox


def choose_task(parent,start):
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr('解包并恢复'))
    dialog.resize(500,390)
    layout = QVBoxLayout(dialog)
    hint = QLabel(tr('请将解包清单与整套压缩文件放在同一目录。可以同时选择多份清单。'))
    hint.setWordWrap(True)
    layout.addWidget(hint)

    form = QFormLayout()
    layout.addLayout(form)
    manifest_field = QLineEdit(dialog)
    manifest_field.setReadOnly(True)
    manifest_field.setPlaceholderText(tr('选择解包清单（.sortzip.json）'))
    destination_field = QLineEdit(dialog)
    destination_field.setReadOnly(True)
    destination_field.setPlaceholderText(tr('选择输出目录'))
    error = QLabel()
    error.setWordWrap(True)
    error.setTextFormat(Qt.TextFormat.PlainText)
    error.hide()
    paths = []

    def show_error(message):
        error.setText(message)
        error.setVisible(bool(message))

    def refresh():
        begin.setEnabled(bool(paths and destination_field.text()))

    def check_manifest(value):
        path = Path(value)
        if not path.name.lower().endswith(delivery_manifest.EXTENSION):
            raise ValueError(tr('请选择 SortZip 交付清单'))
        delivery_manifest.read(path,delivery_manifest.validate_archive)

    def clear_manifests():
        paths.clear()
        manifest_field.clear()
        manifest_field.setToolTip('')
        refresh()

    def browse_manifests():
        folder = str(Path(paths[0]).parent) if paths else start
        selected,_ = QFileDialog.getOpenFileNames(dialog,tr('选择 SortZip 交付清单'),folder,
                                                  tr('SortZip 交付清单 (*.sortzip.json)'))
        if not selected:
            return
        clear_manifests()
        show_error('')
        for value in selected:
            try:
                check_manifest(value)
            except (OSError,ValueError):
                show_error(tr('无法读取解包清单：{0}',value))
                return
        paths.extend(selected)
        manifest_field.setText(Path(paths[0]).name if len(paths)==1 else tr('已选择 {0} 份清单',len(paths)))
        manifest_field.setToolTip('\n'.join(paths))
        refresh()

    def browse_destination():
        path = QFileDialog.getExistingDirectory(dialog,tr('选择解包输出目录'),destination_field.text() or start)
        if path:
            destination_field.setText(path)
            if paths:
                show_error('')
            refresh()

    for label,field,action in ((tr('解包清单'),manifest_field,browse_manifests),
                               (tr('输出目录'),destination_field,browse_destination)):
        row = QHBoxLayout()
        row.addWidget(field,1)
        browse = QPushButton(tr('浏览'),dialog)
        browse.setAccessibleName(field.placeholderText())
        browse.clicked.connect(action)
        row.addWidget(browse)
        form.addRow(label,row)
    layout.addWidget(error)

    original = StateCheckBox(tr('有原名时恢复文件名'))
    original.setToolTip(tr('自动读取最内层归档中的原名。归档没有提供原名时，保留整理后的文件名。'))
    original.setChecked(True)
    keep = StateCheckBox(tr('保留内层归档'))
    layout.addWidget(original)
    layout.addWidget(keep)
    scroll = QScrollArea(dialog)
    scroll.setWidgetResizable(True)
    content = QWidget(scroll)
    rows = QVBoxLayout(content)
    rows.addStretch()
    scroll.setWidget(content)
    layout.addWidget(scroll)
    editors = []

    def move(row,offset):
        current = next(index for index,(widget,_) in enumerate(editors) if widget is row)
        target = current+offset
        if 0<=target<len(editors):
            editors.insert(target,editors.pop(current))
            rows.removeWidget(row)
            rows.insertWidget(target,row)

    def remove(row):
        index = next(index for index,(widget,_) in enumerate(editors) if widget is row)
        _,editor = editors.pop(index)
        editor.clear()
        rows.removeWidget(row)
        row.deleteLater()

    def add():
        row = QWidget(content)
        fields = QHBoxLayout(row)
        fields.setContentsMargins(0,0,0,0)
        editor = QLineEdit(row)
        editor.setEchoMode(QLineEdit.EchoMode.Password)
        editor.setPlaceholderText(tr('解包密码'))
        fields.addWidget(editor,1)
        for text,label,action in (('↑',tr('上移密码'),lambda:move(row,-1)),
                                  ('↓',tr('下移密码'),lambda:move(row,1)),
                                  ('×',tr('移除密码'),lambda:remove(row))):
            button = QPushButton(text,row)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(action)
            fields.addWidget(button)
        editors.append((row,editor))
        rows.insertWidget(len(editors)-1,row)
        editor.setFocus()

    add_button = QPushButton(tr('添加解包密码'),dialog)
    add_button.clicked.connect(add)
    layout.addWidget(add_button)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel,dialog)
    begin = buttons.button(QDialogButtonBox.StandardButton.Ok)
    begin.setText(tr('开始解包'))
    begin.setEnabled(False)

    def accept():
        if not begin.isEnabled():
            return
        for value in paths:
            try:
                check_manifest(value)
            except (OSError,ValueError):
                show_error(tr('无法读取解包清单：{0}',value))
                clear_manifests()
                return
        if not Path(destination_field.text()).is_dir():
            show_error(tr('请选择有效的输出文件夹'))
            destination_field.clear()
            refresh()
            return
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    result = None
    if dialog.exec()==QDialog.DialogCode.Accepted:
        result = dict(dest=destination_field.text(),unpack_manifests=paths,unpack_keep=keep.isChecked(),
                      unpack_original=original.isChecked(),unpack_passwords=[edit.text() for _,edit in editors if edit.text()])
    for _,edit in editors:
        edit.clear()
    dialog.deleteLater()
    return result
