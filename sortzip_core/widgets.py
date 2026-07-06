import os
import sys

from PySide6.QtWidgets import (
    QLineEdit, QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QTextBrowser,
)
from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from sortzip_core.engine import main_from_config
from sortzip_core.manual_content import MANUAL_TEXT


def resource_path(relative_path):
    if getattr(sys, '_MEIPASS', None):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


def show_styled_dialog(parent, title, message, width=300, height=160):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(width, height)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    title_lbl = QLabel(title)
    title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
    title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title_lbl)
    layout.addSpacing(6)
    msg_lbl = QLabel(message)
    msg_lbl.setWordWrap(True)
    msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(msg_lbl)
    layout.addStretch()
    btn = QPushButton("确定")
    btn.clicked.connect(dlg.accept)
    layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
    dlg.exec()


def show_stats_dialog(parent, stats):
    dlg = QDialog(parent)
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


def show_manual_dialog(parent):
    dlg = QDialog(parent)
    dlg.setWindowTitle("使用手册")
    dlg.resize(520, 460)
    layout = QVBoxLayout(dlg)
    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setHtml(MANUAL_TEXT)
    layout.addWidget(browser)
    btn = QPushButton("关闭")
    btn.clicked.connect(dlg.accept)
    layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
    dlg.exec()


def show_conflict_dialog(parent, folder_name, template, conflicts):
    lines = "\n".join(f"  · {orig} → {name}" for _, orig, name in conflicts)
    msg = (f"文件夹「{folder_name}」中\n"
           f"模板 \"{template}\" 产生以下命名冲突：\n\n"
           f"{lines}\n\n"
           f"请调整命名模板")
    show_styled_dialog(parent, "命名冲突", msg, width=380, height=220)


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
    progress = Signal(int, int, str)
    finished = Signal()

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
            main_from_config(
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
        if msg.startswith("分类"):
            self.stats["files_moved"] = cur
        elif msg.startswith("重命名"):
            self.stats["files_renamed"] = cur
        elif msg.startswith("压缩"):
            self.stats["groups"] = cur
        self.progress.emit(pct, 100, msg)
