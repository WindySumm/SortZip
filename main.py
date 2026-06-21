import sys
import os
from pathlib import Path

def _resource_path(relative_path):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QCheckBox, QPushButton, QTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QMessageBox, QFileDialog,
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot, QSettings
from PySide6.QtGui import QIcon, QTextCursor

import SortZip


class LogStream:
    def __init__(self, signal):
        self.signal = signal

    def write(self, text):
        if text:
            self.signal.emit(text)

    def flush(self):
        pass


class Worker(QObject):
    log = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, config):
        super().__init__()
        self.config = config

    @Slot()
    def run(self):
        old_stdout = sys.stdout
        sys.stdout = LogStream(self.log)
        try:
            SortZip.main_from_config(self.config)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            sys.stdout = old_stdout
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SortZip - 文件分类压缩工具")
        self.setWindowIcon(QIcon(_resource_path("icon.png")))
        self.setMinimumSize(800, 680)

        self.settings = QSettings("SortZip", "SortZip")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)

        # ---- 基本设置 ----
        basic_group = QGroupBox("基本设置")
        basic_form = QFormLayout(basic_group)

        self.src_edit = QLineEdit(self.settings.value("src", ""))
        self.src_btn = QPushButton("浏览")
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        src_row.addWidget(self.src_btn)
        basic_form.addRow("源文件夹:", src_row)

        self.dest_edit = QLineEdit(self.settings.value("dest", ""))
        self.dest_btn = QPushButton("浏览")
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit)
        dest_row.addWidget(self.dest_btn)
        basic_form.addRow("目标目录:", dest_row)

        self.group_size_spin = QSpinBox()
        self.group_size_spin.setRange(1, 9999)
        self.group_size_spin.setValue(1)
        basic_form.addRow("每包文件数:", self.group_size_spin)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["文件名", "修改时间"])
        basic_form.addRow("排序方式:", self.sort_combo)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空表示无密码")
        self.pwd_toggle_btn = QPushButton("显示")
        self.pwd_toggle_btn.setFixedWidth(40)
        self.pwd_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pwd_toggle_btn.clicked.connect(self._toggle_password)
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self.password_edit)
        pwd_row.addWidget(self.pwd_toggle_btn)
        basic_form.addRow("压缩密码:", pwd_row)

        self.volume_edit = QLineEdit()
        self.volume_edit.setPlaceholderText("留空自动检测，如 100m / 1g")
        basic_form.addRow("分卷大小:", self.volume_edit)

        layout.addWidget(basic_group)

        # ---- 扩展名映射 ----
        ext_group = QGroupBox("扩展名映射")
        ext_layout = QVBoxLayout(ext_group)

        self.ext_table = QTableWidget(0, 2)
        self.ext_table.setHorizontalHeaderLabels(["扩展名", "文件夹名"])
        self.ext_table.setMinimumHeight(140)
        self.ext_table.horizontalHeader().setStretchLastSection(True)
        self.ext_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.ext_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        ext_layout.addWidget(self.ext_table)

        ext_btn_row = QHBoxLayout()
        self.ext_add_btn = QPushButton("添加")
        self.ext_del_btn = QPushButton("删除选中")
        ext_btn_row.addWidget(self.ext_add_btn)
        ext_btn_row.addWidget(self.ext_del_btn)
        ext_btn_row.addStretch()
        ext_layout.addLayout(ext_btn_row)

        # 预置映射
        for ext, name in [
            (".mp4", "视频"),
            (".mkv", "视频"),
            (".mp3", "音乐"),
            (".flac", "音乐"),
            (".txt", "文档"),
            (".jpg", "图片"),
            (".png", "图片"),
        ]:
            self._add_ext_row(ext, name)

        layout.addWidget(ext_group, 1)

        # ---- 选项 ----
        opt_group = QGroupBox("选项")
        opt_layout = QHBoxLayout(opt_group)

        self.keep_cb = QCheckBox("保留原始文件")
        self.double_cb = QCheckBox("二次打包 ( .zipp )")
        self.double_cb.setChecked(True)
        self.auto_close_cb = QCheckBox("自动关闭 Bandizip 窗口")
        self.auto_close_cb.setChecked(True)

        opt_layout.addWidget(self.keep_cb)
        opt_layout.addWidget(self.double_cb)
        opt_layout.addWidget(self.auto_close_cb)
        opt_layout.addStretch()

        layout.addWidget(opt_group)

        # ---- 执行 ----
        self.run_btn = QPushButton("开始执行")
        self.run_btn.setMinimumHeight(40)
        layout.addWidget(self.run_btn)

        # ---- 日志 ----
        log_label = QLabel("输出日志:")
        layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(160)
        layout.addWidget(self.log_text, 0)

        # ---- 信号连接 ----
        self.src_btn.clicked.connect(lambda: self._browse_folder(self.src_edit))
        self.dest_btn.clicked.connect(lambda: self._browse_folder(self.dest_edit))
        self.ext_add_btn.clicked.connect(lambda: self._add_ext_row("", ""))
        self.ext_del_btn.clicked.connect(self._del_ext_row)
        self.run_btn.clicked.connect(self._run)

    # ---- helpers ----
    def _save_settings(self):
        self.settings.setValue("src", self.src_edit.text())
        self.settings.setValue("dest", self.dest_edit.text())

    def _toggle_password(self):
        if self.password_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.pwd_toggle_btn.setText("隐藏")
        else:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pwd_toggle_btn.setText("显示")

    def _browse_folder(self, edit):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if folder:
            edit.setText(folder)

    def _add_ext_row(self, ext, name):
        row = self.ext_table.rowCount()
        self.ext_table.insertRow(row)
        self.ext_table.setItem(row, 0, QTableWidgetItem(ext))
        self.ext_table.setItem(row, 1, QTableWidgetItem(name))

    def _del_ext_row(self):
        rows = set(i.row() for i in self.ext_table.selectedIndexes())
        for r in sorted(rows, reverse=True):
            self.ext_table.removeRow(r)

    def _build_config(self):
        custom_names = {}
        for r in range(self.ext_table.rowCount()):
            ext_item = self.ext_table.item(r, 0)
            name_item = self.ext_table.item(r, 1)
            if ext_item and name_item and ext_item.text().strip():
                custom_names[ext_item.text().strip()] = name_item.text().strip()

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
        }

    def _run(self):
        config = self._build_config()

        # 校验
        if not config['src'] or not os.path.isdir(config['src']):
            QMessageBox.warning(self, "错误", "请选择有效的源文件夹")
            return
        if config['password'] and len(config['password']) < 8:
            QMessageBox.warning(self, "密码格式错误",
                                "压缩密码至少需要 8 位字符\n"
                                "支持字母、数字和特殊符号\n"
                                "如不需要密码请留空")
            return
        self.run_btn.setEnabled(False)
        self.log_text.clear()

        self.thread = QThread()
        self.worker = Worker(config)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.finished.connect(self._save_settings)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self.run_btn.setEnabled(True))
        self.worker.log.connect(self._append_log)
        self.worker.error.connect(lambda e: self._append_log(f"[错误] {e}"))

        self.thread.start()

    @Slot(str)
    def _append_log(self, text):
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        self.log_text.insertPlainText(text)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
