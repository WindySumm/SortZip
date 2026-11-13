import os
import sys

from PySide6.QtWidgets import (
    QLineEdit, QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QTextBrowser, QTableWidget, QFormLayout,
)
from PySide6.QtCore import Qt, QObject, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from sortzip_core.config import validate_password
from sortzip_core.tasks import Worker, LogStream  # backward-compatible imports
from sortzip_core.manual_content import MANUAL_TEXT


def resource_path(relative_path):
    if getattr(sys, '_MEIPASS', None):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)




class PasswordDialog(QDialog):
    """复用主界面密码区块的交互：密码 + 确认密码 + 显示/隐藏切换 + 长度/一致性校验。"""

    def __init__(self, parent=None, initial="", hint=""):
        super().__init__(parent)
        self.setWindowTitle("输入压缩密码")
        self.setFixedSize(340, 230)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title_lbl = QLabel("输入压缩密码")
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_lbl)

        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setStyleSheet("color: #888888; font-size: 12px;")
            hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(hint_lbl)

        form = QFormLayout()
        form.setSpacing(8)

        self.password_edit = QLineEdit(initial)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空表示无密码")
        self.confirm_edit = QLineEdit(initial)
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.setPlaceholderText("再次输入压缩密码")
        self.pwd_toggle_btn = QPushButton("显示")
        self.pwd_toggle_btn.setFixedWidth(40)
        self.pwd_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pwd_toggle_btn.clicked.connect(self._toggle_password)
        confirm_row = QHBoxLayout()
        confirm_row.addWidget(self.confirm_edit)
        confirm_row.addWidget(self.pwd_toggle_btn)

        form.addRow("压缩密码:", self.password_edit)
        form.addRow("确认密码:", confirm_row)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setStyleSheet("background: #0078d4; color: white; padding: 4px 16px;")
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _toggle_password(self):
        if self.password_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.pwd_toggle_btn.setText("隐藏")
        else:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pwd_toggle_btn.setText("显示")

    def _on_ok(self):
        err = validate_password(self.password_edit.text(), self.confirm_edit.text())
        if err:
            show_styled_dialog(self, err[0], err[1])
            return
        self.accept()

    def password(self):
        return self.password_edit.text()


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


def show_stats_dialog(parent, stats, dest_path=None, status="completed"):
    dlg = QDialog(parent)
    dlg.setWindowTitle("统计报告")
    dlg.setMinimumSize(340, 300)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    titles = {
        "completed": "处理成功完成",
        "safe_cancelled": "已安全中断",
        "force_cancelled": "已强制中断",
        "error": "处理中断（异常）",
        "partial": "部分分组跳过（可恢复）",
    }
    title = QLabel(titles.get(status, "处理完成"))
    title.setStyleSheet("font-size: 14px; font-weight: bold;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)
    layout.addSpacing(6)
    if stats.get('error_detail'):
        detail = QLabel(stats['error_detail'])
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(detail)
    for label, key in [("分类文件", "files_moved"),
                       ("重命名文件", "files_renamed"),
                       ("成功组数", "groups"), ("失败组数", "groups_failed"),
                       ("跳过组数", "groups_skipped"), ("待处理组数", "groups_pending")]:
        row = QHBoxLayout()
        lbl = QLabel(f"{label}:")
        val_text = str(stats.get(key, 0))
        if key == "groups" and stats.get("groups_total"):
            val_text = f"{stats.get('groups', 0)}/{stats['groups_total']}"
        val = QLabel(val_text)
        val.setStyleSheet("font-weight: bold;")
        row.addStretch()
        row.addWidget(lbl)
        row.addWidget(val)
        row.addStretch()
        layout.addLayout(row)
    layout.addStretch()
    btn_layout = QHBoxLayout()
    if dest_path:
        open_btn = QPushButton("打开输出文件夹")
        open_btn.clicked.connect(lambda: os.startfile(dest_path))
        btn_layout.addWidget(open_btn)
    ok_btn = QPushButton("确定")
    ok_btn.clicked.connect(dlg.accept)
    btn_layout.addWidget(ok_btn)
    layout.addLayout(btn_layout)
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


class ReorderableTable(QTableWidget):
    rowsReordered = Signal()
    rowsAboutToReorder = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setDragDropMode(QTableWidget.DragDrop.InternalMove)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):
        if event.source() != self or not self.selectedIndexes():
            super().dropEvent(event)
            return
        drag_row = self.selectedIndexes()[0].row()
        drop_pos = self.indexAt(event.position().toPoint())
        if not drop_pos.isValid():
            super().dropEvent(event)
            return
        drop_row = drop_pos.row()
        if drag_row == drop_row:
            event.accept()
            return
        self.rowsAboutToReorder.emit()
        blocked = self.blockSignals(True)
        row_data = []
        for col in range(self.columnCount()):
            item = self.takeItem(drag_row, col)
            row_data.append(item)
        self.removeRow(drag_row)
        insert_row = drop_row if drop_row < drag_row else drop_row
        self.insertRow(insert_row)
        for col, item in enumerate(row_data):
            self.setItem(insert_row, col, item)
        self.blockSignals(blocked)
        event.accept()
        self.rowsReordered.emit()


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


class DropResumeButton(QPushButton):
    resumeDropped = Signal(str)
    resumeDragEntered = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith("sortzip_resume.json"):
                    event.acceptProposedAction()
                    self.resumeDragEntered.emit()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path.endswith("sortzip_resume.json"):
                    self.resumeDropped.emit(path)
                    event.acceptProposedAction()
                    return
        super().dropEvent(event)
