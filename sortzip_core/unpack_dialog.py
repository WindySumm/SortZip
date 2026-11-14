"""Collect local extraction choices and an ordered, task-only password list."""
from PySide6.QtWidgets import (QDialog,QDialogButtonBox,QFileDialog,QHBoxLayout,
                               QLineEdit,QPushButton,QScrollArea,QVBoxLayout,QWidget)

from sortzip_core.i18n import tr
from sortzip_core.ui_controls import StateCheckBox


def choose_task(parent,start):
    paths,_ = QFileDialog.getOpenFileNames(parent,tr('选择 SortZip 交付清单'),start,
                                        tr('SortZip 交付清单 (*.sortzip.json)'))
    if not paths:
        return None
    destination = QFileDialog.getExistingDirectory(parent,tr('选择解包输出目录'),start)
    if not destination:
        return None
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr('解包并恢复'))
    dialog.resize(470,360)
    layout = QVBoxLayout(dialog)
    original = StateCheckBox(tr('有映射时恢复原名'))
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
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    result = None
    if dialog.exec()==QDialog.DialogCode.Accepted:
        result = dict(dest=destination,unpack_manifests=paths,unpack_keep=keep.isChecked(),
                      unpack_original=original.isChecked(),unpack_passwords=[edit.text() for _,edit in editors if edit.text()])
    for _,edit in editors:
        edit.clear()
    dialog.deleteLater()
    return result
