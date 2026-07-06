import os
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QCheckBox, QPushButton, QProgressBar, QTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog, QScrollArea, QGridLayout,
    QStackedWidget,
)
from PySide6.QtCore import Qt, QThread, Slot, QSettings, QUrl
from PySide6.QtGui import QIcon, QTextCursor, QDesktopServices

from sortzip_core.constants import EXT_CATEGORIES, DARK_QSS, validate_win_folder_name
from sortzip_core.widgets import (
    resource_path, show_styled_dialog, show_stats_dialog,
    DropLineEdit, Worker,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SortZip - 文件分类压缩工具")
        icon_path = resource_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(640, 600)

        self.settings = QSettings("SortZip", "SortZip")
        self.thread = None
        self.worker = None

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ======== 左侧导航栏 ========
        sidebar = QWidget()
        sidebar.setFixedWidth(120)
        sidebar.setProperty("sidebar", True)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 16, 8, 16)
        sidebar_layout.setSpacing(4)

        self.sidebar_btns = []
        for i, text in enumerate(["文件", "映射", "开始", "设置"]):
            btn = QPushButton(text)
            btn.setFixedHeight(40)
            btn.setProperty("active", "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
            sidebar_layout.addWidget(btn)
            self.sidebar_btns.append(btn)

        sidebar_layout.addStretch()

        sidebar.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 8px 14px;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                background-color: transparent;
            }
            QPushButton:hover {
                background-color: #e0e0e0;
            }
            QPushButton[active="true"] {
                background-color: #0078d4;
                color: white;
                font-weight: bold;
            }
        """)

        main_layout.addWidget(sidebar)

        # ======== 右侧内容区（页面栈） ========
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        self._build_file_page()
        self._build_map_page()
        self._build_start_page()
        self._build_settings_page()

        self._switch_page(0)
        self._toggle_theme()

        # ======== 信号连接 ========
        self.src_btn.clicked.connect(lambda: self._browse_folder(self.src_edit))
        self.dest_btn.clicked.connect(lambda: self._browse_folder(self.dest_edit))
        self.ext_add_btn.clicked.connect(lambda: self._add_ext_row("", ""))
        self.ext_del_btn.clicked.connect(self._del_ext_row)
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn.clicked.connect(self._cancel)

    # ==================== 页面构建 ====================

    def _build_file_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        basic_group = QGroupBox("基本设置")
        basic_form = QFormLayout(basic_group)

        self.src_edit = DropLineEdit(self.settings.value("src", ""))
        self.src_btn = QPushButton("浏览")
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        src_row.addWidget(self.src_btn)
        basic_form.addRow("源文件夹:", src_row)

        self.dest_edit = DropLineEdit(self.settings.value("dest", ""))
        self.dest_btn = QPushButton("浏览")
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit)
        dest_row.addWidget(self.dest_btn)
        basic_form.addRow("目标目录:", dest_row)

        self.group_size_spin = QSpinBox()
        self.group_size_spin.setRange(1, 9999)
        self.group_size_spin.setValue(int(self.settings.value("group_size", 1)))
        basic_form.addRow("每包文件数:", self.group_size_spin)

        sort_map_load = {"文件名": 0, "修改时间": 1}
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["文件名", "修改时间"])
        saved_sort = self.settings.value("sort_by", "文件名")
        self.sort_combo.setCurrentIndex(sort_map_load.get(saved_sort, 0))
        basic_form.addRow("排序方式:", self.sort_combo)

        self.password_edit = QLineEdit(self.settings.value("password", ""))
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空表示无密码")
        basic_form.addRow("压缩密码:", self.password_edit)

        self.password_confirm_edit = QLineEdit(self.settings.value("password", ""))
        self.password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirm_edit.setPlaceholderText("再次输入压缩密码")
        self.pwd_toggle_btn = QPushButton("显示")
        self.pwd_toggle_btn.setFixedWidth(40)
        self.pwd_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pwd_toggle_btn.clicked.connect(self._toggle_password)
        pwd_confirm_row = QHBoxLayout()
        pwd_confirm_row.addWidget(self.password_confirm_edit)
        pwd_confirm_row.addWidget(self.pwd_toggle_btn)
        basic_form.addRow("确认密码:", pwd_confirm_row)

        self.volume_edit = QLineEdit(self.settings.value("volume", ""))
        self.volume_edit.setPlaceholderText("留空自动检测")
        basic_form.addRow("分卷大小:", self.volume_edit)

        layout.addWidget(basic_group)

        self.no_rename_cb = QCheckBox("不进行重命名（保留原文件名）")
        self.no_rename_cb.setChecked(self.settings.value("skip_rename", False, type=bool))
        layout.addWidget(self.no_rename_cb)

        layout.addStretch()
        self.stack.addWidget(page)

    def _build_map_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        enabled_group = QGroupBox("已启用映射")
        enabled_layout = QVBoxLayout(enabled_group)

        self.ext_table = QTableWidget(0, 3)
        self.ext_table.setHorizontalHeaderLabels(["启用", "扩展名", "文件夹名"])
        self.ext_table.setMinimumHeight(100)
        self.ext_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.ext_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setStretchLastSection(False)
        self.ext_table.cellChanged.connect(self._on_table_cell_changed)
        self.ext_table.cellPressed.connect(self._on_ext_cell_pressed)
        self._edit_old_folder_name = ""
        enabled_layout.addWidget(self.ext_table, 1)

        ext_btn_row = QHBoxLayout()
        self.ext_add_btn = QPushButton("添加")
        self.ext_del_btn = QPushButton("删除选中")
        ext_btn_row.addWidget(self.ext_add_btn)
        ext_btn_row.addWidget(self.ext_del_btn)
        ext_btn_row.addStretch()
        enabled_layout.addLayout(ext_btn_row)

        layout.addWidget(enabled_group)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        picker_content = QWidget()
        picker_layout = QVBoxLayout(picker_content)
        picker_layout.setSpacing(8)

        self._ext_pickers = {}

        for cat_name, exts in EXT_CATEGORIES.items():
            cat_group = QGroupBox(cat_name)
            cat_grid = QGridLayout(cat_group)
            for i, ext in enumerate(exts):
                chk = QCheckBox(ext)
                chk.stateChanged.connect(
                    lambda state, e=ext, c=cat_name: self._on_ext_picker_toggled(e, c, state == 2)
                )
                cat_grid.addWidget(chk, i // 3, i % 3)
                self._ext_pickers[ext] = chk
            picker_layout.addWidget(cat_group)

        picker_layout.addStretch()
        scroll.setWidget(picker_content)
        layout.addWidget(scroll, 1)

        self._load_ext_state()

        self.stack.addWidget(page)

    def _build_start_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        opt_group = QGroupBox("选项")
        opt_layout = QHBoxLayout(opt_group)

        self.keep_cb = QCheckBox("保留原始文件")
        self.keep_cb.setChecked(self.settings.value("keep_files", False, type=bool))
        self.double_cb = QCheckBox("二次打包 ( .zipp )")
        self.double_cb.setChecked(self.settings.value("double_compress", True, type=bool))
        self.auto_close_cb = QCheckBox("自动关闭 Bandizip 窗口")
        self.auto_close_cb.setChecked(self.settings.value("auto_close", True, type=bool))

        opt_layout.addWidget(self.keep_cb)
        opt_layout.addWidget(self.double_cb)
        opt_layout.addWidget(self.auto_close_cb)
        opt_layout.addStretch()

        layout.addWidget(opt_group)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(24)
        progress_row.addWidget(self.progress_bar, 1)

        self.run_btn = QPushButton("开始执行")
        self.run_btn.setMinimumHeight(40)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setMinimumHeight(40)
        self.cancel_btn.setEnabled(False)
        progress_row.addWidget(self.run_btn)
        progress_row.addWidget(self.cancel_btn)
        layout.addLayout(progress_row)

        layout.addWidget(QLabel("输出日志:"))

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

        self.stack.addWidget(page)

    def _build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        theme_group = QGroupBox("主题设置")
        theme_layout = QVBoxLayout(theme_group)
        self.dark_mode_cb = QCheckBox("深色模式")
        self.dark_mode_cb.setChecked(self.settings.value("dark_mode", False, type=bool))
        self.dark_mode_cb.toggled.connect(self._toggle_theme)
        theme_layout.addWidget(self.dark_mode_cb)
        layout.addWidget(theme_group)

        about_group = QGroupBox("关于")
        about_layout = QVBoxLayout(about_group)
        ver_label = QLabel("版本: v0.3.5")
        about_layout.addWidget(ver_label)
        self.github_btn = QPushButton("打开 GitHub 仓库")
        self.github_btn.clicked.connect(self._open_github)
        about_layout.addWidget(self.github_btn)
        layout.addWidget(about_group)
        layout.addStretch()
        self.stack.addWidget(page)

    # ==================== 页面切换 ====================

    def _switch_page(self, index):
        for i, btn in enumerate(self.sidebar_btns):
            btn.setProperty("active", "true" if i == index else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.stack.setCurrentIndex(index)
        if index == 1:
            self._resize_ext_columns()

    def _toggle_theme(self):
        qss = DARK_QSS if self.dark_mode_cb.isChecked() else ""
        QApplication.instance().setStyleSheet(qss)

    def _open_github(self):
        QDesktopServices.openUrl(QUrl("https://github.com/WindySumm/SortZip"))

    # ==================== 持久化 ====================

    def _save_settings(self):
        self.settings.setValue("src", self.src_edit.text())
        self.settings.setValue("dest", self.dest_edit.text())
        self.settings.setValue("group_size", self.group_size_spin.value())
        self.settings.setValue("sort_by", self.sort_combo.currentText())
        self.settings.setValue("password", self.password_edit.text())
        self.settings.setValue("volume", self.volume_edit.text())
        self.settings.setValue("keep_files", self.keep_cb.isChecked())
        self.settings.setValue("double_compress", self.double_cb.isChecked())
        self.settings.setValue("auto_close", self.auto_close_cb.isChecked())
        self.settings.setValue("skip_rename", self.no_rename_cb.isChecked())
        self.settings.setValue("dark_mode", self.dark_mode_cb.isChecked())
        self._save_ext_state()

    def _save_ext_state(self):
        self.settings.beginWriteArray("ext_mappings")
        for r in range(self.ext_table.rowCount()):
            self.settings.setArrayIndex(r)
            chk_item = self.ext_table.item(r, 0)
            ext_item = self.ext_table.item(r, 1)
            name_item = self.ext_table.item(r, 2)
            self.settings.setValue("checked", chk_item.checkState() == Qt.CheckState.Checked)
            self.settings.setValue("ext", ext_item.text() if ext_item else "")
            self.settings.setValue("name", name_item.text() if name_item else "")
        self.settings.endArray()

    def _load_ext_state(self):
        count = self.settings.beginReadArray("ext_mappings")
        if count == 0:
            self.settings.endArray()
            return
        for r in range(count):
            self.settings.setArrayIndex(r)
            checked = self.settings.value("checked", False, type=bool)
            ext = self.settings.value("ext", "")
            name = self.settings.value("name", "")
            if ext:
                self._add_ext_row(ext, name, checked=checked)
        self.settings.endArray()
        self._sync_pickers_from_table()

    # ==================== 辅助方法 ====================

    def _toggle_password(self):
        if self.password_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.pwd_toggle_btn.setText("隐藏")
        else:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pwd_toggle_btn.setText("显示")

    def _browse_folder(self, edit):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if folder:
            edit.setText(folder)

    def _resize_ext_columns(self):
        header = self.ext_table.horizontalHeader()
        total = header.width()
        chk_w = header.sectionSize(0)
        avail = total - chk_w
        if avail > 40:
            header.resizeSection(1, int(avail * 1 / 3))
            header.resizeSection(2, int(avail * 2 / 3))

    # ==================== 扩展映射相关 ====================

    def _add_ext_row(self, ext, name, checked=False):
        row = self.ext_table.rowCount()
        self.ext_table.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.ext_table.setItem(row, 0, chk)
        self.ext_table.setItem(row, 1, QTableWidgetItem(ext))
        self.ext_table.setItem(row, 2, QTableWidgetItem(name))

    def _del_ext_row(self):
        rows = set(i.row() for i in self.ext_table.selectedIndexes())
        for r in sorted(rows, reverse=True):
            ext_item = self.ext_table.item(r, 1)
            if ext_item:
                ext = ext_item.text().strip()
                if ext in self._ext_pickers:
                    self._ext_pickers[ext].blockSignals(True)
                    self._ext_pickers[ext].setChecked(False)
                    self._ext_pickers[ext].blockSignals(False)
            self.ext_table.removeRow(r)

    def _on_ext_picker_toggled(self, ext, category, checked):
        self.ext_table.blockSignals(True)
        if checked:
            found = False
            for r in range(self.ext_table.rowCount()):
                item = self.ext_table.item(r, 1)
                if item and item.text().strip() == ext:
                    chk = self.ext_table.item(r, 0)
                    if chk:
                        chk.setCheckState(Qt.CheckState.Checked)
                    found = True
                    break
            if not found:
                self._add_ext_row(ext, category, checked=True)
        else:
            for r in range(self.ext_table.rowCount() - 1, -1, -1):
                item = self.ext_table.item(r, 1)
                if item and item.text().strip() == ext:
                    self.ext_table.removeRow(r)
                    break
        self.ext_table.blockSignals(False)

    def _on_table_cell_changed(self, row, col):
        if col == 0:
            chk_item = self.ext_table.item(row, 0)
            ext_item = self.ext_table.item(row, 1)
            if not ext_item:
                return
            ext = ext_item.text().strip()
            checked = chk_item and chk_item.checkState() == Qt.CheckState.Checked
            if ext in self._ext_pickers:
                self._ext_pickers[ext].blockSignals(True)
                self._ext_pickers[ext].setChecked(checked)
                self._ext_pickers[ext].blockSignals(False)
            if not checked:
                self.ext_table.blockSignals(True)
                self.ext_table.removeRow(row)
                self.ext_table.blockSignals(False)
        elif col == 2:
            self._validate_folder_cell(row, col)

    def _on_ext_cell_pressed(self, row, col):
        if col == 2:
            self._edit_old_folder_name = ""
            item = self.ext_table.item(row, col)
            if item:
                self._edit_old_folder_name = item.text()

    def _validate_folder_cell(self, row, col):
        item = self.ext_table.item(row, col)
        if not item:
            return
        name = item.text().strip()
        error = validate_win_folder_name(name)
        if error:
            show_styled_dialog(self, "文件夹名无效", error, width=360, height=180)
            self.ext_table.blockSignals(True)
            item.setText(getattr(self, '_edit_old_folder_name', ''))
            self.ext_table.blockSignals(False)

    def _sync_pickers_from_table(self):
        for chk in self._ext_pickers.values():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        for r in range(self.ext_table.rowCount()):
            chk_item = self.ext_table.item(r, 0)
            ext_item = self.ext_table.item(r, 1)
            if chk_item and ext_item and chk_item.checkState() == Qt.CheckState.Checked:
                ext = ext_item.text().strip()
                if ext in self._ext_pickers:
                    self._ext_pickers[ext].blockSignals(True)
                    self._ext_pickers[ext].setChecked(True)
                    self._ext_pickers[ext].blockSignals(False)

    # ==================== 执行流程 ====================

    def _build_config(self):
        custom_names = {}
        for r in range(self.ext_table.rowCount()):
            chk_item = self.ext_table.item(r, 0)
            ext_item = self.ext_table.item(r, 1)
            name_item = self.ext_table.item(r, 2)
            if (chk_item and chk_item.checkState() == Qt.CheckState.Checked
                    and ext_item and ext_item.text().strip()):
                custom_names[ext_item.text().strip()] = name_item.text().strip() if name_item else ""

        vol = self.volume_edit.text().strip() or None
        sort_map = {"文件名": "name", "修改时间": "mtime"}

        return {
            'src': self.src_edit.text().strip(),
            'dest': self.dest_edit.text().strip(),
            'group_size': self.group_size_spin.value(),
            'password': self.password_edit.text(),
            'volume': vol,
            'bandizip': 'bandizip',
            'custom_names': custom_names,
            'sort_by': sort_map.get(self.sort_combo.currentText(), 'name'),
            'keep_files': self.keep_cb.isChecked(),
            'double_compress': self.double_cb.isChecked(),
            'auto_close': self.auto_close_cb.isChecked(),
            'skip_rename': self.no_rename_cb.isChecked(),
        }

    def _run(self):
        config = self._build_config()

        if not config['src'] or not os.path.isdir(config['src']):
            show_styled_dialog(self, "错误", "请选择有效的源文件夹")
            return
        if config['password'] and len(config['password']) < 8:
            show_styled_dialog(self, "密码格式错误",
                               "压缩密码至少需要 8 位字符\n"
                               "支持字母、数字和特殊符号\n"
                               "如不需要密码请留空",
                               width=320, height=180)
            return
        if config['password'] != self.password_confirm_edit.text():
            show_styled_dialog(self, "密码不一致",
                               "两次输入的压缩密码不一致\n请重新输入",
                               width=300, height=160)
            return

        custom_names = config.get('custom_names', {})
        if custom_names and config['dest'] and os.path.isdir(config['dest']):
            existing_dirs = set(d.name for d in Path(config['dest']).iterdir() if d.is_dir())
            mapped_folders = set(custom_names.values())
            overlap = existing_dirs & mapped_folders
            if overlap:
                show_styled_dialog(self, "目标文件夹冲突",
                                   "目标文件夹中已存在以下映射目录名：\n" +
                                   "\n".join(f"  · {name}" for name in sorted(overlap)) +
                                   "\n\n请先清理目标文件夹或修改映射名称",
                                   width=340, height=200)
                return

        if custom_names and config['src'] and os.path.isdir(config['src']):
            src_exts = set(f.suffix.lower() for f in Path(config['src']).iterdir() if f.is_file())
            checked_exts = set(custom_names.keys())
            if not src_exts & checked_exts:
                show_styled_dialog(self, "无匹配文件",
                                   "源文件夹中没有符合已勾选扩展名的文件\n"
                                   "请检查源文件夹或勾选正确的扩展名",
                                   width=320, height=170)
                return

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()

        self.thread = QThread()
        self.worker = Worker(config)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(self._save_settings)
        self.worker.finished.connect(self._on_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.worker.log.connect(self._append_log)
        self.worker.error.connect(lambda e: self._append_log(f"[错误] {e}"))
        self.worker.progress.connect(self._update_progress)

        self.thread.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self._append_log("正在取消...")

    @Slot(int, int, str)
    def _update_progress(self, value, maximum, text):
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(f"{text}  [{value}%]")

    def _on_finished(self):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("完成  [100%]")
        stats = self.worker.stats if self.worker else {}
        show_stats_dialog(self, stats)

    @Slot(str)
    def _append_log(self, text):
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        self.log_text.insertPlainText(text)

    # ==================== 窗口事件 ====================

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_ext_columns()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_ext_columns()

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
