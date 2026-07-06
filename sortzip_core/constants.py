# ---- 扩展名分类定义 ----
EXT_CATEGORIES = {
    "视频": [
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
        ".m4v", ".3gp", ".rmvb", ".mpg", ".mpeg", ".vob", ".ts",
    ],
    "音乐": [
        ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a",
        ".wma", ".opus", ".aiff", ".mid", ".midi", ".amr",
    ],
    "图片": [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
        ".tiff", ".tif", ".raw", ".psd", ".heic", ".ico",
    ],
    "文档": [
        ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
        ".csv", ".json", ".xml", ".md", ".html", ".htm",
        ".log", ".epub", ".rtf", ".yaml", ".yml", ".ini",
    ],
    "压缩包": [
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    ],
    "程序": [
        ".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh",
        ".py", ".js", ".vbs", ".dll",
    ],
    "字体": [
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ],
}

# ---- Windows 文件夹名校验 ----
_INVALID_FOLDER_CHARS = set(r'\/:*?"<>|')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | \
    {f"COM{i}" for i in range(1, 10)} | \
    {f"LPT{i}" for i in range(1, 10)}


def validate_win_folder_name(name):
    if not name:
        return "文件夹名不能为空"
    for c in name:
        if c in _INVALID_FOLDER_CHARS:
            return f"文件夹名不能包含字符：{c}"
    if name[-1] in (' ', '.'):
        return "文件夹名不能以空格或句点结尾"
    if name.upper() in _RESERVED_NAMES:
        return f"文件夹名不能是 Windows 保留名称：{name}"
    return None


# ---- 命名模板预设 ----
RENAME_PRESETS = [
    ("{n}{ext}",              "纯序号 (默认)"),
    ("{folder}_{n}{ext}",     "文件夹_序号"),
    ("{original}{ext}",       "保留原文件名"),
    ("{original}_{n}{ext}",   "原文件名_序号"),
]

# ---- 暗色主题样式表 ----
DARK_QSS = """
QMainWindow, QDialog, QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
}
QGroupBox {
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    margin-top: 12px;
    font-weight: bold;
    color: #e0e0e0;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: #e0e0e0;
}
QLabel {
    color: #e0e0e0;
    background-color: transparent;
}
QLineEdit, QSpinBox, QComboBox, QTextEdit, QTextBrowser {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 4px;
    selection-background-color: #094771;
}
QTextBrowser {
    background-color: #1e1e1e;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    image: none;
}
QComboBox QAbstractItemView {
    background-color: #2d2d2d;
    color: #e0e0e0;
    selection-background-color: #094771;
    selection-color: #e0e0e0;
    border: 1px solid #3c3c3c;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: #2d2d2d;
    border: 1px solid #3c3c3c;
    border-radius: 2px;
    width: 18px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #3c3c3c;
}
QSpinBox::up-arrow, QSpinBox::down-arrow {
    width: 0;
    height: 0;
}
QCheckBox {
    color: #e0e0e0;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #3c3c3c;
    border-radius: 3px;
    background-color: #2d2d2d;
}
QCheckBox::indicator:checked {
    background-color: #0078d4;
    border-color: #0078d4;
}
QCheckBox::indicator:hover {
    border-color: #0078d4;
}
QPushButton {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #3c3c3c;
    border-color: #0078d4;
}
QPushButton:pressed {
    background-color: #094771;
}
QPushButton:disabled {
    color: #555;
    background-color: #252525;
    border-color: #2d2d2d;
}
QProgressBar {
    background-color: #2d2d2d;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    text-align: center;
    color: #e0e0e0;
}
QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 3px;
}
QTableWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    gridline-color: #3c3c3c;
    border: 1px solid #3c3c3c;
    selection-background-color: #094771;
    selection-color: #e0e0e0;
    outline: none;
}
QTableWidget::item {
    padding: 4px;
    border: none;
}
QTableWidget::item:selected {
    background-color: #094771;
    color: #e0e0e0;
}
QHeaderView {
    background-color: #1e1e1e;
    border: none;
}
QHeaderView::section {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #3c3c3c;
    border-left: none;
    border-top: none;
    padding: 4px;
    font-weight: bold;
}
QScrollArea {
    background-color: #1e1e1e;
    border: none;
}
QScrollBar:vertical {
    background-color: #1e1e1e;
    width: 12px;
    border: none;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #3c3c3c;
    border-radius: 6px;
    min-height: 20px;
    margin: 2px;
}
QScrollBar::handle:vertical:hover {
    background-color: #4c4c4c;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
    border: none;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}
QScrollBar:horizontal {
    background-color: #1e1e1e;
    height: 12px;
    border: none;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: #3c3c3c;
    border-radius: 6px;
    min-width: 20px;
    margin: 2px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #4c4c4c;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
    border: none;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}
QTabBar {
    background-color: #1e1e1e;
    border: none;
}
QTabBar::tab {
    background-color: #2d2d2d;
    color: #a0a0a0;
    border: 1px solid #3c3c3c;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #1e1e1e;
    color: #e0e0e0;
    border-bottom: 1px solid #1e1e1e;
}
QTabBar::tab:hover:!selected {
    background-color: #3c3c3c;
    color: #e0e0e0;
}
"""
