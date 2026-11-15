"""Collect local extraction choices and an ordered, task-only password list."""
from hashlib import sha256
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,
                               QLabel,QLineEdit,QPushButton,QScrollArea,QVBoxLayout,QWidget)

from sortzip_core import delivery_manifest
from sortzip_core.i18n import tr
from sortzip_core.ui_controls import StateCheckBox


def choose_task(parent,start):
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr('解包并恢复'))
    dialog.resize(500,390)
    layout = QVBoxLayout(dialog)
    hint = QLabel(tr('请选择清单和输出目录。压缩包须保留相对清单的位置。'))
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
    manifests = {}
    retained = {}
    task_groups = {}
    task_choices = {}
    task_retention = {}
    task_original = {}
    task_available = {}

    def available_groups(path,groups):
        found = set()
        for index,group in enumerate(groups):
            folder = Path(path).parent/group['folder']
            if all((folder/row['shown']).is_file() or (folder/row['canonical']).is_file()
                   for row in group['tree']['archive']['volumes']):
                found.add(index)
        return found

    def check_manifest(value):
        path = Path(value)
        if not path.name.lower().endswith(delivery_manifest.EXTENSION):
            raise ValueError(tr('请选择 SortZip 交付清单'))
        manifest = delivery_manifest.read(path,delivery_manifest.validate_delivery)
        return (manifest.get('layer_count'),sha256(delivery_manifest.encode(manifest)).hexdigest(),
                manifest.get('groups'),manifest.get('passwords',{}).get('mode'))

    def show_error(message):
        error.setText(message)
        error.setVisible(bool(message))

    def refresh():
        begin.setEnabled(bool(destination_field.text() and any(
            path not in task_groups or task_choices[path] for path in manifests)))

    def clear_manifests():
        manifests.clear()
        retained.clear()
        task_groups.clear()
        task_choices.clear()
        task_retention.clear()
        task_original.clear()
        task_available.clear()
        selector.clear()
        render_layers()
        manifest_field.clear()
        manifest_field.setToolTip('')
        refresh()

    def browse_manifests():
        folder = str(Path(next(iter(manifests))).parent) if manifests else start
        selected,_ = QFileDialog.getOpenFileNames(dialog,tr('选择 SortZip 交付清单'),folder,
                                                  tr('SortZip 交付清单 (*.sortzip.json)'))
        if not selected:
            return
        show_error('')
        checked = {}
        for value in selected:
            try:
                checked[value] = check_manifest(value)
            except (OSError,ValueError):
                clear_manifests()
                show_error(tr('无法读取解包清单：{0}',value))
                return
        previous = {path:retained[path] for path in checked if checked[path]==manifests.get(path)}
        clear_manifests()
        manifests.update(checked)
        names = [Path(value).name.casefold() for value in manifests]
        for value,(count,_,groups,_) in manifests.items():
            retained[value] = previous.get(value,False if count is None else [])
            if groups is not None:
                task_groups[value] = groups
                task_available[value] = available_groups(value,groups)
                task_choices[value] = set(task_available[value])
                task_retention[value] = {}
                task_original[value] = {}
            name = Path(value).name
            selector.addItem(value if names.count(name.casefold())>1 else name,value)
            selector.setItemData(selector.count()-1,value,Qt.ItemDataRole.ToolTipRole)
        selector.setVisible(len(manifests)>1)
        render_layers()
        manifest_field.setText(Path(selected[0]).name if len(manifests)==1 else tr('已选择 {0} 份清单',len(manifests)))
        manifest_field.setToolTip('\n'.join(manifests))
        refresh()

    def browse_destination():
        path = QFileDialog.getExistingDirectory(dialog,tr('选择解包输出目录'),destination_field.text() or start)
        if path:
            destination_field.setText(path)
            if manifests:
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
    layout.addWidget(original)
    selector = QComboBox(dialog)
    selector.setAccessibleName(tr('解包清单'))
    selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    selector.hide()
    layout.addWidget(selector)
    retention = QWidget(dialog)
    retention_layout = QVBoxLayout(retention)
    retention_layout.setContentsMargins(0,0,0,0)
    layout.addWidget(retention)
    resume_hint = QLabel(tr('恢复未完成的解包时，沿用原任务的保留设置。'))
    resume_hint.setWordWrap(True)
    resume_hint.hide()
    layout.addWidget(resume_hint)

    def render_layers():
        while retention_layout.count():
            widget = retention_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        value = selector.currentData()
        original.setVisible(value not in task_groups)
        if value in task_groups:
            render_task_groups(value)
            return
        count = manifests[value][0] if value in manifests else 1
        has_options = count!=1
        retention.setVisible(has_options)
        resume_hint.setVisible(has_options)
        if not has_options:
            return
        if count is None:
            keep = StateCheckBox(tr('保留内层归档'))
            keep.setChecked(retained[value])
            keep.toggled.connect(lambda checked:retained.__setitem__(value,checked))
            retention_layout.addWidget(keep)
            return
        bar = QWidget(retention)
        actions = QHBoxLayout(bar)
        actions.setContentsMargins(0,0,0,0)
        actions.addWidget(QLabel(tr('保留内层归档')))
        actions.addStretch()
        layer_scroll = QScrollArea(retention)
        layer_scroll.setWidgetResizable(True)
        layer_scroll.setMaximumHeight(104)
        layer_content = QWidget(layer_scroll)
        layer_layout = QVBoxLayout(layer_content)
        layer_layout.setContentsMargins(8,4,8,4)
        layer_scroll.setWidget(layer_content)
        boxes = []

        def toggle_layer(layer,checked):
            layers = retained[value]
            if checked:
                layers.append(layer)
            else:
                layers.remove(layer)

        for layer in range(1,count):
            box = StateCheckBox(tr('第 {0} 层',layer))
            box.setChecked(layer in retained[value])
            box.toggled.connect(lambda checked,number=layer:toggle_layer(number,checked))
            boxes.append(box)
            layer_layout.addWidget(box)

        def set_all(checked):
            for box in boxes:
                box.setChecked(checked)

        for label,checked in ((tr('保留全部内层'),True),(tr('全部清除'),False)):
            button = QPushButton(label,bar)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked,keep=checked:set_all(keep))
            actions.addWidget(button)
        retention_layout.addWidget(bar)
        retention_layout.addWidget(layer_scroll)

    def has_original(tree):
        return (any('original' in row for row in tree['members'].get('files',()))
                or any(has_original(child) for child in tree['children']))

    def render_task_groups(value):
        retention.show()
        resume_hint.show()
        scroll_groups = QScrollArea(retention)
        scroll_groups.setWidgetResizable(True)
        scroll_groups.setMaximumHeight(180)
        content = QWidget(scroll_groups)
        rows = QVBoxLayout(content)
        scroll_groups.setWidget(content)
        retention_layout.addWidget(scroll_groups)
        for index,group in enumerate(task_groups[value]):
            archive = group['tree']['archive']
            available = index in task_available[value]
            selected = task_choices[value]
            kept = task_retention[value].setdefault(index,[])
            path = '/'.join(filter(None,(group['folder'],archive['entry'])))
            checkbox = StateCheckBox(path if available else tr('缺卷：{0}',path))
            checkbox.setChecked(index in selected)
            checkbox.setEnabled(available)
            rows.addWidget(checkbox)
            layer_boxes = []

            def choose(checked,number=index,boxes=layer_boxes):
                (selected.add if checked else selected.discard)(number)
                for box in boxes:
                    box.setEnabled(checked)
                refresh()

            checkbox.toggled.connect(choose)
            if has_original(group['tree']):
                rename = StateCheckBox(tr('有原名时恢复文件名'))
                rename.setEnabled(checkbox.isChecked())
                rename.setChecked(task_original[value].setdefault(index,True))
                rename.toggled.connect(lambda checked,number=index:task_original[value].__setitem__(number,checked))
                rows.addWidget(rename)
                layer_boxes.append(rename)
            for layer in range(1,group['layer_count']):
                box = StateCheckBox(tr('保留第 {0} 层',layer))
                box.setEnabled(checkbox.isChecked())
                box.setChecked(layer in kept)
                box.toggled.connect(lambda checked,number=layer,levels=kept:
                                    levels.append(number) if checked else levels.remove(number))
                rows.addWidget(box)
                layer_boxes.append(box)

    selector.currentIndexChanged.connect(render_layers)
    render_layers()
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
    phrases = {}

    def accept():
        if not begin.isEnabled():
            return
        for value,reviewed in manifests.items():
            try:
                current = check_manifest(value)
            except (OSError,ValueError):
                show_error(tr('无法读取解包清单：{0}',value))
                clear_manifests()
                return
            if current!=reviewed:
                show_error(tr('解包清单已更改，请重新选择：{0}',value))
                clear_manifests()
                return
        if not Path(destination_field.text()).is_dir():
            show_error(tr('请选择有效的输出文件夹'))
            destination_field.clear()
            refresh()
            return
        from PySide6.QtWidgets import QInputDialog
        for value,(_,_,groups,mode) in manifests.items():
            if groups is None or mode!='encrypted' or not task_choices[value]:
                continue
            phrase,accepted = QInputDialog.getText(dialog,tr('交付口令'),Path(value).name,
                                                    QLineEdit.EchoMode.Password)
            if not accepted or not phrase:
                phrases.clear()
                return
            phrases[value] = phrase
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    result = None
    if dialog.exec()==QDialog.DialogCode.Accepted:
        result = dict(dest=destination_field.text(),unpack_manifests=list(manifests),unpack_retention=retained,
                      unpack_manifest_keys={path:digest for path,(_,digest,_,_) in manifests.items()},
                      unpack_task_choices={path:sorted(indices) for path,indices in task_choices.items()},
                      unpack_task_retention={path:{str(index):levels for index,levels in groups.items()}
                                             for path,groups in task_retention.items()},
                      unpack_task_original={path:{str(index):enabled for index,enabled in groups.items()}
                                            for path,groups in task_original.items()},
                      unpack_original=original.isChecked(),unpack_passwords=[edit.text() for _,edit in editors if edit.text()])
        if phrases:
            result['unpack_delivery_phrases'] = phrases
    for _,edit in editors:
        edit.clear()
    dialog.deleteLater()
    return result
