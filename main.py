import sys
import os
from pathlib import Path

# ---- 工具函数：获取资源文件路径（支持开发环境和 PyInstaller 打包后） ----
def _resource_path(relative_path):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QCheckBox, QPushButton, QProgressBar, QTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QMessageBox, QFileDialog, QDialog,
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot, QSettings
from PySide6.QtGui import QIcon, QTextCursor, QDragEnterEvent, QDropEvent

import SortZip


# ---- 支持拖入文件夹的输入框 ----
class DropLineEdit(QLineEdit):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                if os.path.isdir(path):
                    self.setText(path)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# ---- 日志流：将 print 输出重定向到 Qt 信号 ----
class LogStream:
    def __init__(self, signal):
        self.signal = signal

    def write(self, text):
        if text:
            self.signal.emit(text)

    def flush(self):
        pass


# ---- 后台工作线程：执行 SortZip 核心逻辑，不阻塞 UI ----
class Worker(QObject):
    log = Signal(str)          # 日志信号
    error = Signal(str)        # 错误信号
    progress = Signal(int, int, str)  # 进度：(当前值, 最大值, 描述)
    finished = Signal()    # 完成信号

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._cancelled = False
        self.stats = {"files_moved": 0, "files_renamed": 0, "groups": 0}

    def cancel(self):
        self._cancelled = True

    def _cancel_check(self):
        return self._cancelled

    @Slot()
    def run(self):
        old_stdout = sys.stdout
        sys.stdout = LogStream(self.log)
        try:
            SortZip.main_from_config(
                self.config,
                on_progress=lambda start, end, cur, total, msg: self._report_progress(start, end, cur, total, msg),
                cancel_check=self._cancel_check,
            )
        except Exception as e:
            self.error.emit(str(e))
        finally:
            sys.stdout = old_stdout
            self.finished.emit()

    def _report_progress(self, start_pct, end_pct, cur, total, msg):
        if total <= 0:
            pct = start_pct
        else:
            pct = start_pct + (end_pct - start_pct) * cur // total
        # 根据阶段统计
        if msg.startswith("分类"):
            self.stats["files_moved"] = cur
        elif msg.startswith("重命名"):
            self.stats["files_renamed"] = cur
        elif msg.startswith("压缩"):
            self.stats["groups"] = cur
        self.progress.emit(pct, 100, msg)


# ---- 主窗口 ----
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ---- 窗口基本属性 ----
        self.setWindowTitle("SortZip - 文件分类压缩工具")
        icon_path = _resource_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(640, 680)

        # ---- 本地持久化配置 ----
        self.settings = QSettings("SortZip", "SortZip")

        # ---- 中央容器 ----
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)

        # ======== 基本设置区域 ========
        basic_group = QGroupBox("基本设置")
        basic_form = QFormLayout(basic_group)

        # 源文件夹
        self.src_edit = DropLineEdit(self.settings.value("src", ""))
        self.src_btn = QPushButton("浏览")
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        src_row.addWidget(self.src_btn)
        basic_form.addRow("源文件夹:", src_row)

        # 目标输出目录
        self.dest_edit = DropLineEdit(self.settings.value("dest", ""))
        self.dest_btn = QPushButton("浏览")
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit)
        dest_row.addWidget(self.dest_btn)
        basic_form.addRow("目标目录:", dest_row)

        # 每包文件数
        self.group_size_spin = QSpinBox()
        self.group_size_spin.setRange(1, 9999)
        self.group_size_spin.setValue(int(self.settings.value("group_size", 1)))
        basic_form.addRow("每包文件数:", self.group_size_spin)

        # 排序方式
        sort_map_load = {"文件名": 0, "修改时间": 1}
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["文件名", "修改时间"])
        saved_sort = self.settings.value("sort_by", "文件名")
        self.sort_combo.setCurrentIndex(sort_map_load.get(saved_sort, 0))
        basic_form.addRow("排序方式:", self.sort_combo)

        # 压缩密码（带显示/隐藏切换）
        self.password_edit = QLineEdit(self.settings.value("password", ""))
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

        # 手动分卷大小
        self.volume_edit = QLineEdit(self.settings.value("volume", ""))
        self.volume_edit.setPlaceholderText("留空自动检测")
        basic_form.addRow("分卷大小:", self.volume_edit)

        layout.addWidget(basic_group)

        # ======== 扩展名映射区域 ========
        ext_group = QGroupBox("扩展名映射")
        ext_layout = QVBoxLayout(ext_group)

        # 映射表格：启用勾选 | 扩展名 | 文件夹名
        self.ext_table = QTableWidget(0, 3)
        self.ext_table.setHorizontalHeaderLabels(["启用", "扩展名", "文件夹名"])
        self.ext_table.setMinimumHeight(118)
        self.ext_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.ext_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setStretchLastSection(False)
        ext_layout.addWidget(self.ext_table, 1)

        # 表格操作按钮
        ext_btn_row = QHBoxLayout()
        self.ext_add_btn = QPushButton("添加")
        self.ext_del_btn = QPushButton("删除选中")
        ext_btn_row.addWidget(self.ext_add_btn)
        ext_btn_row.addWidget(self.ext_del_btn)
        ext_btn_row.addStretch()
        ext_layout.addLayout(ext_btn_row)

        # 首次启动加载预置映射，后续从 QSettings 恢复用户状态
        if not self._load_ext_state():
            for ext, name in [
                # ---- 视频 ----
                (".mp4", "视频"),
                (".mkv", "视频"),
                (".avi", "视频"),
                (".mov", "视频"),
                (".wmv", "视频"),
                (".flv", "视频"),
                (".webm", "视频"),
                # ---- 音乐 ----
                (".mp3", "音乐"),
                (".flac", "音乐"),
                (".wav", "音乐"),
                (".aac", "音乐"),
                (".ogg", "音乐"),
                (".m4a", "音乐"),
                # ---- 图片 ----
                (".jpg", "图片"),
                (".jpeg", "图片"),
                (".png", "图片"),
                (".gif", "图片"),
                (".bmp", "图片"),
                (".webp", "图片"),
                (".svg", "图片"),
                # ---- 文档 ----
                (".txt", "文档"),
                (".doc", "文档"),
                (".docx", "文档"),
                (".xls", "文档"),
                (".xlsx", "文档"),
                (".ppt", "文档"),
                (".pptx", "文档"),
                (".pdf", "文档"),
                # ---- 压缩包 ----
                (".zip", "压缩包"),
                (".rar", "压缩包"),
                (".7z", "压缩包"),
            ]:
                self._add_ext_row(ext, name, checked=False)

        layout.addWidget(ext_group, 1)

        # ======== 选项区域 ========
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

        # ======== 进度条 + 执行 / 取消按钮 ========
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

        # ======== 日志输出区域 ========
        log_label = QLabel("输出日志:")
        layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(160)
        layout.addWidget(self.log_text, 0)

        # ======== 信号连接 ========
        self.src_btn.clicked.connect(lambda: self._browse_folder(self.src_edit))
        self.dest_btn.clicked.connect(lambda: self._browse_folder(self.dest_edit))
        self.ext_add_btn.clicked.connect(lambda: self._add_ext_row("", ""))
        self.ext_del_btn.clicked.connect(self._del_ext_row)
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn.clicked.connect(self._cancel)

    # ======== 私有辅助方法 ========

    # ---- 持久化：保存所有设置项 ----
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
        self._save_ext_state()

    # ---- 密码显示 / 隐藏切换 ----
    def _toggle_password(self):
        if self.password_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.pwd_toggle_btn.setText("隐藏")
        else:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pwd_toggle_btn.setText("显示")

    # ---- 保存扩展名映射表状态到 QSettings ----
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

    # ---- 从 QSettings 恢复扩展名映射表状态 ----
    def _load_ext_state(self):
        count = self.settings.beginReadArray("ext_mappings")
        if count == 0:
            self.settings.endArray()
            return False
        for r in range(count):
            self.settings.setArrayIndex(r)
            checked = self.settings.value("checked", False, type=bool)
            ext = self.settings.value("ext", "")
            name = self.settings.value("name", "")
            self._add_ext_row(ext, name, checked=checked)
        self.settings.endArray()
        return True

    # ---- 浏览文件夹 ----
    def _browse_folder(self, edit):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if folder:
            edit.setText(folder)

    # ---- 向映射表添加一行 ----
    def _add_ext_row(self, ext, name, checked=False):
        row = self.ext_table.rowCount()
        self.ext_table.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.ext_table.setItem(row, 0, chk)
        self.ext_table.setItem(row, 1, QTableWidgetItem(ext))
        self.ext_table.setItem(row, 2, QTableWidgetItem(name))

    # ---- 删除映射表中选中的行 ----
    def _del_ext_row(self):
        rows = set(i.row() for i in self.ext_table.selectedIndexes())
        for r in sorted(rows, reverse=True):
            self.ext_table.removeRow(r)

    # ---- 从 UI 控件取值，构建配置字典 ----
    def _build_config(self):
        custom_names = {}
        for r in range(self.ext_table.rowCount()):
            chk_item = self.ext_table.item(r, 0)
            ext_item = self.ext_table.item(r, 1)
            name_item = self.ext_table.item(r, 2)
            # 只收集勾选了的行
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
        }

    # ---- 执行主流程 ----
    def _run(self):
        config = self._build_config()

        # 校验输入
        if not config['src'] or not os.path.isdir(config['src']):
            QMessageBox.warning(self, "错误", "请选择有效的源文件夹")
            return
        if config['password'] and len(config['password']) < 8:
            QMessageBox.warning(self, "密码格式错误",
                                "压缩密码至少需要 8 位字符\n"
                                "支持字母、数字和特殊符号\n"
                                "如不需要密码请留空")
            return

        # 检查：目标文件夹中是否已存在映射目录名
        custom_names = config.get('custom_names', {})
        if custom_names and config['dest'] and os.path.isdir(config['dest']):
            existing_dirs = set(d.name for d in Path(config['dest']).iterdir() if d.is_dir())
            mapped_folders = set(custom_names.values())
            overlap = existing_dirs & mapped_folders
            if overlap:
                QMessageBox.warning(self, "目标文件夹冲突",
                                    "目标文件夹中已存在以下映射目录名：\n" +
                                    "\n".join(f"  · {name}" for name in sorted(overlap)) +
                                    "\n\n请先清理目标文件夹或修改映射名称")
                return

        # 检查：源文件夹中是否有符合勾选类型的文件
        if custom_names and config['src'] and os.path.isdir(config['src']):
            src_exts = set(f.suffix.lower() for f in Path(config['src']).iterdir() if f.is_file())
            checked_exts = set(custom_names.keys())
            if not src_exts & checked_exts:
                QMessageBox.warning(self, "无匹配文件",
                                    "源文件夹中没有符合已勾选扩展名的文件\n"
                                    "请检查源文件夹或勾选正确的扩展名")
                return

        # 准备执行
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()

        # 创建工作线程
        self.thread = QThread()
        self.worker = Worker(config)
        self.worker.moveToThread(self.thread)

        # 信号绑定
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

    # ---- 取消操作 ----
    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self._append_log("正在取消...")

    # ---- 进度更新 ----
    @Slot(int, int, str)
    def _update_progress(self, value, maximum, text):
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(f"{text}  [{value}%]")

    # ---- 执行完成 ----
    def _on_finished(self):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("完成  [100%]")

        stats = self.worker.stats if self.worker else {}
        dlg = QDialog(self)
        dlg.setWindowTitle("统计报告")
        dlg.setFixedSize(280, 180)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        title = QLabel("处理成功完成")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(6)
        for label, key in [("移动文件", "files_moved"),
                           ("重命名文件", "files_renamed"),
                           ("压缩组数", "groups")]:
            row = QHBoxLayout()
            lbl = QLabel(f"{label}:")
            val = QLabel(str(stats.get(key, 0)))
            val.setStyleSheet("font-weight: bold;")
            row.addStretch()
            row.addWidget(lbl)
            row.addWidget(val)
            row.addStretch()
            layout.addLayout(row)
        layout.addStretch()
        btn = QPushButton("确定")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        dlg.exec()

    # ---- 按 1:2 比例调整扩展名列与文件夹名列宽度 ----
    def _resize_ext_columns(self):
        header = self.ext_table.horizontalHeader()
        total = header.width()
        chk_w = header.sectionSize(0)
        avail = total - chk_w
        if avail > 40:
            header.resizeSection(1, int(avail * 1 / 3))
            header.resizeSection(2, int(avail * 2 / 3))

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_ext_columns()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_ext_columns()

    # ---- 窗口关闭时保存映射状态 ----
    def closeEvent(self, event):
        self._save_ext_state()
        super().closeEvent(event)

    # ---- 向日志面板追加文本 ----
    @Slot(str)
    def _append_log(self, text):
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)
        self.log_text.insertPlainText(text)


# ---- 程序入口 ----
def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
