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
QMainWindow, QDialog {
    background-color: #2b2b2b;
    color: #f0f0f0;
}
QGroupBox {
    border: 1px solid #555;
    border-radius: 6px;
    margin-top: 12px;
    font-weight: bold;
    color: #f0f0f0;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}
QLabel {
    color: #f0f0f0;
}
QLineEdit, QSpinBox, QComboBox, QTextEdit {
    background-color: #3c3c3c;
    color: #f0f0f0;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background-color: #3c3c3c; color: #f0f0f0;
    selection-background-color: #0078d4;
}
QCheckBox { color: #f0f0f0; }
QPushButton {
    background-color: #3c3c3c; color: #f0f0f0;
    border: 1px solid #555; border-radius: 4px; padding: 6px 14px;
}
QPushButton:hover { background-color: #4c4c4c; border-color: #0078d4; }
QPushButton:disabled { color: #666; }
QProgressBar {
    background-color: #3c3c3c; border: 1px solid #555;
    border-radius: 4px; text-align: center; color: #f0f0f0;
}
QProgressBar::chunk { background-color: #0078d4; border-radius: 3px; }
QTableWidget, QHeaderView {
    background-color: #2b2b2b; color: #f0f0f0;
    gridline-color: #555; border: 1px solid #555;
}
QHeaderView::section {
    background-color: #3c3c3c; color: #f0f0f0;
    border: 1px solid #555; padding: 4px;
}
QScrollArea { background-color: #2b2b2b; border: none; }
QScrollBar:vertical {
    background-color: #2b2b2b; width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #555; border-radius: 6px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
