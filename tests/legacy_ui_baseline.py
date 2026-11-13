"""Historical UI regression fixture; never imported by the shipped application.

Its assertions preserve the pre-workbench baseline. Production behavior is also
covered through tests/test_workbench.py against the actual default entry.
"""
import os
import sys
import json
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QCheckBox, QPushButton, QProgressBar, QTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog, QScrollArea, QGridLayout,
    QStackedWidget, QTabBar, QDialog, QInputDialog,
)
from PySide6.QtCore import Qt, QThread, Slot, Signal, QTimer, QSettings, QUrl
from PySide6.QtGui import QIcon, QTextCursor, QDesktopServices, QFont

from sortzip_core.constants import EXT_CATEGORIES, DARK_QSS, RENAME_PRESETS, validate_win_folder_name, RESUME_FILE_NAME
from sortzip_core.engine import _match_rule, check_naming_conflicts, render_template, _sort_files, _load_checkpoint, _checkpoint_path, validate_resume_files
from sortzip_core import __version__
from sortzip_core.formats import FORMATS, validate_compression
from sortzip_core.planning import build_execution_plan
from sortzip_core.config import prepare_config, ConfigurationError
from sortzip_core.nesting import Layer, plan_layers
from sortzip_core.layer_editor import LayerEditor, LayerStack, public_layers
from sortzip_core.layer_migration import migrate_legacy
from sortzip_core.ui_controls import StateCheckBox
from sortzip_core.tasks import Worker, create_worker_thread
from sortzip_core.credentials import WindowsCredentialStore, load_password, save_password
from sortzip_core.widgets import (
    resource_path, show_styled_dialog, show_stats_dialog,
    show_manual_dialog, show_conflict_dialog,
    ReorderableTable, DropLineEdit, DropResumeButton,
    PasswordDialog, validate_password,
)


SORT_MAP = {
    "文件名(升序)": "name", "文件名(降序)": "name_desc",
    "修改时间(旧→新)": "mtime", "修改时间(新→旧)": "mtime_desc",
    "文件大小(小→大)": "size_asc", "文件大小(大→小)": "size_desc",
    "扩展名": "ext",
}


class UpdateChecker(QThread):
    check_done = Signal(str)

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            import json
            import urllib.request
            url = "https://api.github.com/repos/WindySumm/SortZip/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "SortZip"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if not self.isInterruptionRequested():
                    self.check_done.emit(data.get("tag_name", ""))
        except Exception:
            if not self.isInterruptionRequested():
                self.check_done.emit("")


def _compare_versions(v1, v2):
    try:
        def parse(v):
            return tuple(int(x) for x in v.lstrip("vV").split("."))
        return parse(v1) > parse(v2)
    except Exception:
        return False


class MainWindow(QMainWindow):
    def __init__(self, settings=None, credential_store=None, check_updates=True):
        super().__init__()
        self.setFont(QFont('Microsoft YaHei UI',10))
        self.setWindowTitle(f"SortZip {__version__} - 文件整理与压缩")
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(640, 600)

        self.settings = settings if settings is not None else QSettings("SortZip", "SortZip")
        self._credential_store = credential_store if credential_store is not None else WindowsCredentialStore()
        self._password_warnings = set()
        self._saved_password = load_password(self.settings, self._credential_store, self._password_warning)
        self.thread = None
        self.worker = None
        self._pending_result = None
        self._close_pending = False
        self._preview_order = {}
        self._compression_layers = (Layer(),)
        self._missing_layer_passwords = set()
        self._layer_migration_invalid = False
        try:
            saved = json.loads(self.settings.value('compression_layers', '[]'))
            if saved and (self.settings.value('unified_layers',False,type=bool) or
                          self.settings.value('nested_enabled',False,type=bool)):
                self._compression_layers = tuple(Layer(**{k:v for k,v in row.items() if k!='password'}) for row in saved)
                plan_layers((), self._compression_layers)
                required = json.loads(self.settings.value('layer_password_required', '[]'))
                self._missing_layer_passwords = {i for i,value in enumerate(required) if value}
            else:
                legacy = {key:self.settings.value(key,default,type=kind) for key,default,kind in (
                    ('first_compress',True,bool),('double_compress',False,bool),('group_size',1,int),
                    ('archive_format','zip',str),('enable_volume',False,bool),('volume','',str),
                    ('verify_archive',False,bool),('remember_pwd',False,bool),
                    ('first_rename_enable',False,bool),('first_suffix_replace','.zip',str),
                    ('archive_rename_enable',False,bool),('archive_suffix','.zip',str))}
                legacy['password'] = self._saved_password
                migrated = migrate_legacy(legacy)
                self._compression_layers = migrated.layers
                self._missing_layer_passwords = {i for i,needed in enumerate(migrated.password_required)
                    if needed and not migrated.layers[i].password}
                if migrated.notices:
                    message = '\n'.join(migrated.notices)
                    QTimer.singleShot(0,lambda message=message:show_styled_dialog(self,'层设置迁移',message))
        except (ValueError, TypeError, AttributeError):
            self._compression_layers = (Layer(),)
            self._layer_migration_invalid = True
            QTimer.singleShot(0, lambda:show_styled_dialog(self,'层设置无效','无法迁移旧设置，请在压缩卡片中重新配置并确认。'))
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._refresh_preview)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ======== 左侧导航栏 ========
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(120)
        self.sidebar.setProperty("sidebar", True)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(8, 16, 8, 16)
        sidebar_layout.setSpacing(4)

        self.sidebar_btns = []
        for i, text in enumerate(["文件", "映射", "命名", "压缩", "设置"]):
            btn = QPushButton(text)
            btn.setFixedHeight(40)
            btn.setProperty("active", "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
            sidebar_layout.addWidget(btn)
            self.sidebar_btns.append(btn)

        sidebar_layout.addStretch()

        self.sidebar.setStyleSheet("""
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

        main_layout.addWidget(self.sidebar)

        # ======== 右侧内容区（页面栈） ========
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        self._build_file_page()
        self._build_map_page()
        self._build_naming_page()
        self._build_start_page()
        self._build_settings_page()

        self._switch_page(0)
        self._toggle_theme()

        # ======== 信号连接 ========
        self.src_btn.clicked.connect(lambda: self._browse_folder(self.src_edit))
        self.dest_btn.clicked.connect(lambda: self._browse_folder(self.dest_edit))
        self.ext_add_btn.clicked.connect(lambda: self._add_ext_row("", ""))
        self.ext_del_btn.clicked.connect(self._del_ext_row)
        self.ext_all_btn.clicked.connect(self._enable_all_mappings)
        self.ext_del_all_btn.clicked.connect(self._del_all_ext_rows)
        self.naming_add_btn.clicked.connect(lambda: self._add_naming_row("", ""))
        self.naming_del_btn.clicked.connect(self._del_naming_row)
        self.enable_volume_cb.toggled.connect(self._on_enable_volume_toggled)
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn.clicked.connect(self._cancel)
        self.resume_btn.clicked.connect(self._import_resume)
        self.resume_btn.resumeDropped.connect(self._start_resume)
        self.resume_btn.resumeDragEntered.connect(lambda: None)

        self.remember_pwd_cb.toggled.connect(self._save_password)
        self.nested_cb.toggled.connect(self._on_nested_toggled)
        self.layers_btn.clicked.connect(self._edit_layers)
        for edit in (self.src_edit, self.dest_edit, self.archive_suffix_edit, self.first_suffix_edit):
            edit.textChanged.connect(self._schedule_preview)
        for checkbox in (self.recursive_cb, self.keep_hierarchy_cb, self.first_cb, self.double_cb,
                         self.first_rename_cb, self.archive_rename_cb, self.enable_volume_cb, self.output_list_cb):
            checkbox.toggled.connect(self._schedule_preview)
        self.group_size_spin.valueChanged.connect(self._schedule_preview)
        self.compress_format_combo.currentTextChanged.connect(self._schedule_preview)
        self.first_rename_cb.toggled.connect(self._sync_compression_capabilities)
        self.archive_rename_cb.toggled.connect(self._sync_compression_capabilities)
        self._sync_compression_capabilities()
        self._refresh_preview()

        # 恢复窗口位置/尺寸
        geo = self.settings.value("window_geometry")
        if geo:
            self.restoreGeometry(geo)

        # 启动更新检查
        self._update_checker = UpdateChecker()
        self._update_checker.check_done.connect(self._on_update_result)
        if check_updates:
            self._update_checker.start()

    # ==================== 页面构建 ====================

    def _build_file_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 文件选择
        file_sel_group = QGroupBox("文件选择")
        file_sel_layout = QFormLayout(file_sel_group)

        self.src_edit = DropLineEdit(self.settings.value("src", ""))
        self.src_btn = QPushButton("浏览")
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        src_row.addWidget(self.src_btn)
        file_sel_layout.addRow("输入文件夹:", src_row)

        self.dest_edit = DropLineEdit(self.settings.value("dest", ""))
        self.dest_btn = QPushButton("浏览")
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit)
        dest_row.addWidget(self.dest_btn)
        file_sel_layout.addRow("输出文件夹:", dest_row)

        self.recursive_cb = QCheckBox("包含子文件夹")
        self.recursive_cb.setChecked(self.settings.value("recursive", False, type=bool))
        file_sel_layout.addRow("", self.recursive_cb)

        layout.addWidget(file_sel_group)

        # 密码设置
        pwd_group = QGroupBox("密码设置")
        self.password_group = pwd_group
        pwd_layout = QFormLayout(pwd_group)

        self.password_edit = QLineEdit(self._saved_password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("留空表示无密码")
        pwd_layout.addRow("压缩密码:", self.password_edit)

        self.password_confirm_edit = QLineEdit(self._saved_password)
        self.password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirm_edit.setPlaceholderText("再次输入压缩密码")
        self.pwd_toggle_btn = QPushButton("显示")
        self.pwd_toggle_btn.setFixedWidth(40)
        self.pwd_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pwd_toggle_btn.clicked.connect(self._toggle_password)
        pwd_confirm_row = QHBoxLayout()
        pwd_confirm_row.addWidget(self.password_confirm_edit)
        pwd_confirm_row.addWidget(self.pwd_toggle_btn)
        pwd_layout.addRow("确认密码:", pwd_confirm_row)

        self.remember_pwd_cb = QCheckBox("记住密码")
        self.remember_pwd_cb.setChecked(self.settings.value("remember_pwd", False, type=bool))
        if not self.remember_pwd_cb.isChecked():
            self.password_edit.clear()
            self.password_confirm_edit.clear()
        pwd_layout.addRow("", self.remember_pwd_cb)

        layout.addWidget(pwd_group)
        pwd_group.hide()

        # 分卷设置
        volume_group = QGroupBox("分卷设置")
        volume_layout = QFormLayout(volume_group)

        self.group_size_spin = QSpinBox()
        self.group_size_spin.setRange(1, 9999)
        self.group_size_spin.setValue(int(self.settings.value("group_size", 1)))
        volume_layout.addRow("每包文件数:", self.group_size_spin)

        self.volume_edit = QLineEdit(self.settings.value("volume", ""))
        self.volume_edit.setPlaceholderText("留空自动检测")
        volume_layout.addRow("分卷大小:", self.volume_edit)

        self.enable_volume_cb = QCheckBox("启用分卷")
        self.enable_volume_cb.setChecked(self.settings.value("enable_volume", False, type=bool))
        volume_layout.addRow("", self.enable_volume_cb)

        layout.addWidget(volume_group)
        volume_group.hide()

        layout.addStretch()
        self.stack.addWidget(page)

    def _build_map_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        enabled_group = QGroupBox("已启用映射")
        enabled_layout = QVBoxLayout(enabled_group)

        self.ext_table = ReorderableTable(0, 3)
        self.ext_table.setHorizontalHeaderLabels(["启用", "扩展名", "文件夹名"])
        self.ext_table.setMinimumHeight(100)
        self.ext_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.ext_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.ext_table.horizontalHeader().setStretchLastSection(False)
        self.ext_table.cellChanged.connect(self._on_table_cell_changed)
        self.ext_table.cellPressed.connect(self._on_ext_cell_pressed)
        self.ext_table.rowsReordered.connect(self._schedule_preview)
        self._edit_old_folder_name = ""
        enabled_layout.addWidget(self.ext_table, 1)

        ext_btn_row = QHBoxLayout()
        self.ext_all_btn = QPushButton("映射全部")
        self.ext_add_btn = QPushButton("添加")
        self.ext_del_btn = QPushButton("删除选中")
        self.ext_del_all_btn = QPushButton("删除全部")
        ext_btn_row.addWidget(self.ext_all_btn)
        ext_btn_row.addWidget(self.ext_add_btn)
        ext_btn_row.addWidget(self.ext_del_btn)
        ext_btn_row.addWidget(self.ext_del_all_btn)
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

    def _build_naming_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        scroll.setWidget(content)
        page_layout.addWidget(scroll)

        sort_group = QGroupBox("排序设置")
        sort_layout = QFormLayout(sort_group)
        self.sort_combo = QComboBox()
        sort_presets = [
            "文件名(升序)", "文件名(降序)",
            "修改时间(旧→新)", "修改时间(新→旧)",
            "文件大小(小→大)", "文件大小(大→小)",
            "扩展名",
        ]
        self.sort_combo.addItems(sort_presets)
        saved_sort = self.settings.value("sort_by", "文件名(升序)")
        idx = self.sort_combo.findText(saved_sort)
        self.sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.sort_combo.activated.connect(lambda: (self._preview_order.clear(), self._refresh_preview()))
        sort_layout.addRow("文件排序:", self.sort_combo)
        layout.addWidget(sort_group)

        subfolder_group = QGroupBox("子文件夹设置")
        subfolder_layout = QVBoxLayout(subfolder_group)
        self.keep_hierarchy_cb = QCheckBox("保持原文件夹层级")
        self.keep_hierarchy_cb.setChecked(self.settings.value("keep_hierarchy", False, type=bool))
        subfolder_layout.addWidget(self.keep_hierarchy_cb)
        layout.addWidget(subfolder_group)

        rename_group = QGroupBox("分类后重命名")
        rename_layout = QVBoxLayout(rename_group)

        self.naming_table = ReorderableTable(0, 3)
        self.naming_table.setHorizontalHeaderLabels(["启用", "匹配文件夹", "命名模板"])
        self.naming_table.setMinimumHeight(120)
        self.naming_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.naming_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.naming_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.naming_table.cellChanged.connect(self._on_naming_cell_changed)
        self.naming_table.itemChanged.connect(self._on_naming_item_changed)
        self.naming_table.rowsReordered.connect(self._schedule_preview)
        rename_layout.addWidget(self.naming_table, 1)

        naming_btn_row = QHBoxLayout()
        self.naming_add_btn = QPushButton("添加通配规则")
        self.naming_del_btn = QPushButton("删除选中")
        naming_btn_row.addWidget(self.naming_add_btn)
        naming_btn_row.addWidget(self.naming_del_btn)
        naming_btn_row.addStretch()

        naming_btn_row.addWidget(QLabel("预设:"))
        self.naming_preset_combo = QComboBox()
        self.naming_preset_combo.addItems(
            [f"{label}  ({tmpl})" for tmpl, label in RENAME_PRESETS]
        )
        self.naming_preset_combo.setCurrentIndex(-1)
        self.naming_preset_combo.currentIndexChanged.connect(self._on_naming_preset_selected)
        naming_btn_row.addWidget(self.naming_preset_combo)

        rename_layout.addLayout(naming_btn_row)
        layout.addWidget(rename_group)

        preview_group = QGroupBox("效果预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_tab_bar = QTabBar()
        self.preview_tab_bar.setExpanding(False)
        self.preview_tab_bar.currentChanged.connect(self._on_preview_tab_changed)
        preview_layout.addWidget(self.preview_tab_bar)
        self.preview_table = ReorderableTable(0, 3)
        self.preview_table.setHorizontalHeaderLabels(["源文件（相对路径）", "命名后", "所属压缩包"])
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.setMinimumHeight(160)
        self.preview_table.rowsReordered.connect(self._on_preview_reordered)
        preview_layout.addWidget(self.preview_table)
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        layout.addWidget(preview_group)

        compress_rename_group = QGroupBox("压缩后重命名")
        compress_rename_layout = QVBoxLayout(compress_rename_group)

        first_rename_row = QHBoxLayout()
        first_rename_row.addWidget(QLabel("一次压缩后:"))
        first_rename_row.addSpacing(4)
        self.first_rename_cb = QCheckBox("启用后缀名替换")
        self.first_rename_cb.setChecked(self.settings.value("first_rename_enable", False, type=bool))
        first_rename_row.addWidget(self.first_rename_cb)
        first_rename_row.addSpacing(6)
        first_rename_row.addWidget(QLabel("替换为:"))
        self.first_suffix_edit = QLineEdit(
            self.settings.value("first_suffix_replace", f".{self.settings.value('archive_format', 'zip')}")
        )
        self.first_suffix_edit.setPlaceholderText(".zip")
        first_rename_row.addWidget(self.first_suffix_edit)
        first_rename_row.addStretch()
        compress_rename_layout.addLayout(first_rename_row)

        second_rename_row = QHBoxLayout()
        second_rename_row.addWidget(QLabel("二次压缩后:"))
        second_rename_row.addSpacing(4)
        self.archive_rename_cb = QCheckBox("启用后缀名替换")
        self.archive_rename_cb.setChecked(self.settings.value("archive_rename_enable", False, type=bool))
        second_rename_row.addWidget(self.archive_rename_cb)
        second_rename_row.addSpacing(6)
        second_rename_row.addWidget(QLabel("替换为:"))
        self.archive_suffix_edit = QLineEdit(
            self.settings.value("archive_suffix", f".{self.settings.value('archive_format', 'zip')}")
        )
        self.archive_suffix_edit.setPlaceholderText(".zip")
        second_rename_row.addWidget(self.archive_suffix_edit)
        second_rename_row.addStretch()
        compress_rename_layout.addLayout(second_rename_row)

        layout.addWidget(compress_rename_group)
        compress_rename_group.hide()
        layout.addStretch()

        self._load_naming_state()
        self._refresh_preview()

        self.stack.addWidget(page)

    def _build_start_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 压缩设置
        compress_group = QGroupBox("压缩设置")
        compress_layout = QVBoxLayout(compress_group)

        self.first_cb = StateCheckBox("一次压缩")
        self.first_cb.setChecked(self.settings.value("first_compress", True, type=bool))
        self.first_cb.toggled.connect(self._on_first_compress_toggled)
        compress_layout.addWidget(self.first_cb)

        self.double_cb = QCheckBox("二次压缩")
        self.double_cb.setChecked(self.settings.value("double_compress", True, type=bool))
        self.double_cb.toggled.connect(self._on_double_compress_toggled)
        compress_layout.addWidget(self.double_cb)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("压缩格式:"))
        self.compress_format_combo = QComboBox()
        self.compress_format_combo.addItems(
            list(FORMATS)
        )
        saved_fmt = self.settings.value("archive_format", "zip")
        idx = self.compress_format_combo.findText(saved_fmt)
        self.compress_format_combo.setCurrentIndex(idx if idx >= 0 else 0)
        # Unified layer migration handles invalid legacy formats once, visibly.
        self.compress_format_combo.currentTextChanged.connect(self._on_compress_format_changed)
        self.compress_format_combo.setToolTip("压缩格式：仅 ZIP/7Z 支持密码和本工具的分卷流程；GZ/XZ 每组一个文件。")
        fmt_row.addWidget(self.compress_format_combo)
        fmt_row.addStretch()
        compress_layout.addLayout(fmt_row)
        capability_hint = QLabel("ZIP/7Z 支持密码、分卷；其他格式请关闭密码和分卷。\nGZ/XZ 每包只能包含一个文件。后缀替换不改变压缩格式。")
        capability_hint.setWordWrap(True)
        compress_layout.addWidget(capability_hint)

        self.verify_cb = QCheckBox("压缩后校验完整性")
        self.verify_cb.setChecked(self.settings.value("verify_archive", False, type=bool))
        compress_layout.addWidget(self.verify_cb)

        layout.addWidget(compress_group)
        compress_group.hide()

        # 其他选项
        other_group = QGroupBox("执行选项")
        other_layout = QHBoxLayout(other_group)

        self.keep_cb = StateCheckBox("保留原始文件")
        self.keep_cb.setChecked(self.settings.value("keep_files", False, type=bool))
        other_layout.addWidget(self.keep_cb)

        self.output_list_cb = StateCheckBox("输出文件对应清单")
        self.output_list_cb.setChecked(self.settings.value("output_list", False, type=bool))
        other_layout.addWidget(self.output_list_cb)

        self.auto_close_cb = QCheckBox("自动关闭 Bandizip 窗口")
        self.auto_close_cb.setChecked(self.settings.value("auto_close", True, type=bool))
        other_layout.addWidget(self.auto_close_cb)
        self.auto_close_cb.hide()

        layout.addWidget(other_group)

        nested_row = QHBoxLayout()
        self.nested_cb = QCheckBox('启用多层压缩')
        self.nested_cb.setChecked(True)
        self.layers_btn = QPushButton(f'配置压缩层（{len(self._compression_layers)} 层）')
        nested_row.addWidget(self.nested_cb)
        nested_row.addWidget(self.layers_btn)
        layout.addLayout(nested_row)
        self.nested_cb.hide()
        self.layers_btn.hide()

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0,0,0,0)
        title = QLabel('压缩流程')
        title.setStyleSheet('font-size: 18px; font-weight: 600;')
        header_layout.addWidget(title)
        header_layout.addStretch()
        compress_layout.removeWidget(self.first_cb)
        header_layout.addWidget(self.first_cb)
        self.first_cb.setText('启用压缩')
        self.first_cb.show()
        layout.insertWidget(0,header)
        self.layer_stack = LayerStack(self._compression_layers,required=self._missing_layer_passwords)
        try:
            self.layer_stack.restore_preferences(json.loads(self.settings.value('layer_format_preferences','[]')))
        except (ValueError,TypeError):
            pass
        self.layer_stack.changed.connect(self._layers_changed)
        self.layers_scroll = QScrollArea()
        self.layers_scroll.setWidgetResizable(True)
        self.layers_scroll.setWidget(self.layer_stack)
        self.layers_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.layers_scroll.setMinimumHeight(190)
        layout.insertWidget(1,self.layers_scroll,1)
        self.layer_stack.add_btn.hide()
        self.add_layer_btn = QPushButton('＋ 添加一层压缩')
        self.add_layer_btn.clicked.connect(lambda:self.layer_stack.add_layer(Layer()))
        layout.insertWidget(2,self.add_layer_btn)

        # 进度条 + 按钮
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(24)
        progress_row.addWidget(self.progress_bar, 1)

        self.run_btn = QPushButton("开始执行")
        self.run_btn.setStyleSheet('QPushButton {background:#0078d4;color:white;border:0;border-radius:6px;padding:8px 18px;} QPushButton:disabled {background:#888;color:#eee;}')
        self.run_btn.setMinimumHeight(40)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setMinimumHeight(40)
        self.cancel_btn.setEnabled(False)
        self.resume_btn = DropResumeButton("恢复任务（导入断点）")
        self.resume_btn.setMinimumHeight(40)
        self.resume_btn.setToolTip("导入 .sortzip_resume.json 断点文件，从中断处恢复压缩\n支持点击选择或直接拖入文件")
        self.resume_btn.setAcceptDrops(True)
        progress_row.addWidget(self.run_btn)
        progress_row.addWidget(self.cancel_btn)
        layout.addWidget(self.resume_btn)
        layout.addLayout(progress_row)

        self.log_toggle = QPushButton('显示执行日志 ▸')
        self.log_toggle.setCheckable(True)
        layout.addWidget(self.log_toggle)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.hide()
        self.log_toggle.toggled.connect(self.log_text.setVisible)
        self.log_toggle.toggled.connect(lambda checked:self.log_toggle.setText('隐藏执行日志 ▾' if checked else '显示执行日志 ▸'))
        layout.addWidget(self.log_text)

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

        exec_group = QGroupBox("执行选项")
        exec_layout = QVBoxLayout(exec_group)
        self.confirm_config_cb = QCheckBox("执行前确认参数")
        self.confirm_config_cb.setChecked(self.settings.value("confirm_config", True, type=bool))
        exec_layout.addWidget(self.confirm_config_cb)
        layout.addWidget(exec_group)

        manual_group = QGroupBox("使用手册")
        manual_layout = QVBoxLayout(manual_group)
        self.manual_btn = QPushButton("打开使用手册")
        self.manual_btn.clicked.connect(lambda: show_manual_dialog(self))
        manual_layout.addWidget(self.manual_btn)
        layout.addWidget(manual_group)

        reset_group = QGroupBox("恢复")
        reset_layout = QVBoxLayout(reset_group)
        self.reset_btn = QPushButton("还原默认设置")
        self.reset_btn.setStyleSheet("color: #d43b3b;")
        self.reset_btn.clicked.connect(self._reset_defaults)
        reset_layout.addWidget(self.reset_btn)
        layout.addWidget(reset_group)

        about_group = QGroupBox("关于")
        about_layout = QVBoxLayout(about_group)
        ver_label = QLabel(f"版本: {__version__}")
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
        elif index == 2:
            self._sync_naming_from_ext()

    def _toggle_theme(self):
        is_dark = self.dark_mode_cb.isChecked()
        qss = DARK_QSS if is_dark else ""
        QApplication.instance().setStyleSheet(qss)
        hover = "#3c3c3c" if is_dark else "#e0e0e0"
        sidebar_style = f"""
            QPushButton {{
                text-align: left;
                padding: 8px 14px;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                background-color: transparent;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
            QPushButton[active="true"] {{
                background-color: #0078d4;
                color: white;
                font-weight: bold;
            }}
        """
        self.sidebar.setStyleSheet(sidebar_style)
        if hasattr(self,'layer_stack'):
            surface = '#23262c' if is_dark else '#ffffff'
            background = '#1b1e23' if is_dark else '#f5f6f8'
            border = '#3c434f' if is_dark else '#dce1e8'
            accent = '#75bbff' if is_dark else '#006ac6'
            self.layer_stack.setStyleSheet(f'''
                QWidget#LayerStack {{background:{background};}}
                QGroupBox#LayerCard {{background:{surface};border:1px solid {border};border-radius:10px;margin:0;padding:0;}}
                QGroupBox#LayerCard QLabel, QWidget#LayerAdvanced {{background:transparent;}}
                QLabel#LayerBadge {{background:{'#233c53' if is_dark else '#e7f2ff'};color:{accent};padding:2px 6px;border-radius:4px;font-size:9pt;}}
                QLineEdit,QSpinBox,QComboBox {{padding:5px;min-height:20px;}}
                QSpinBox {{border:1px solid {border};border-radius:4px;}}
                QSpinBox QLineEdit {{padding:0;min-height:0;border:0;background:transparent;}}
                QSpinBox QToolButton {{background:transparent;color:{'#e5e7eb' if is_dark else '#374151'};border:0;border-left:1px solid {border};border-radius:0;font-size:14px;}}
                QSpinBox QToolButton:hover {{background:{'#354252' if is_dark else '#e7f2ff'};color:{accent};}}
                QSpinBox QToolButton:pressed {{background:{'#233c53' if is_dark else '#d5e9ff'};}}
                QSpinBox QToolButton:disabled {{color:{'#69717c' if is_dark else '#b6bdc6'};}}
                QPushButton {{padding:5px 10px;min-height:22px;}}
            ''')
            self.add_layer_btn.setStyleSheet(f'QPushButton {{color:{accent};border:1px dashed {border};border-radius:7px;padding:8px;}}')

    def _open_github(self):
        QDesktopServices.openUrl(QUrl("https://github.com/WindySumm/SortZip"))

    def _on_update_result(self, latest_tag):
        if latest_tag and _compare_versions(latest_tag, __version__):
            dlg = QDialog(self)
            dlg.setWindowTitle("发现新版本")
            dlg.setFixedSize(340, 180)
            layout = QVBoxLayout(dlg)
            layout.setSpacing(10)
            title = QLabel("发现新版本")
            title.setStyleSheet("font-size: 14px; font-weight: bold;")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)
            layout.addSpacing(4)
            msg = QLabel(
                f"当前版本：{__version__}\n"
                f"最新版本：{latest_tag}\n\n"
                "是否前往 GitHub 仓库下载更新？"
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(msg)
            layout.addStretch()
            btn_row = QHBoxLayout()
            skip_btn = QPushButton("忽略")
            skip_btn.clicked.connect(dlg.reject)
            go_btn = QPushButton("前往下载")
            go_btn.setStyleSheet("background: #0078d4; color: white; padding: 4px 16px;")
            go_btn.clicked.connect(lambda: (self._open_github(), dlg.accept()))
            btn_row.addStretch()
            btn_row.addWidget(skip_btn)
            btn_row.addWidget(go_btn)
            layout.addLayout(btn_row)
            dlg.exec()

    def _reset_defaults(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("还原默认设置")
        dlg.setFixedSize(340, 160)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)
        title = QLabel("还原默认设置")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(4)
        msg = QLabel("确定要恢复所有设置到默认状态吗？\n此操作将清除所有已保存的配置。")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        layout.addWidget(msg)
        layout.addStretch()
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn = QPushButton("确定还原")
        ok_btn.setStyleSheet("background: #d43b3b; color: white; padding: 4px 16px;")
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        save_password(self.settings, self._credential_store, "", False, self._password_warning)
        self._saved_password = ""
        self.settings.clear()
        self._compression_layers = (Layer(),)
        self._missing_layer_passwords = set()
        self.nested_cb.setChecked(True)
        self.layer_stack.set_layers(self._compression_layers)
        self._layer_migration_invalid = False
        self.layers_btn.setText('配置压缩层（1 层）')
        self._format_lock_volume = False
        self._format_lock_single = False
        self._volume_preference = False
        self._group_size_preference = 1
        self.group_size_spin.setMaximum(9999)
        self.src_edit.setText("")
        self.dest_edit.setText("")
        self.password_edit.clear()
        self.password_confirm_edit.clear()
        self.remember_pwd_cb.setChecked(False)
        self.recursive_cb.setChecked(False)
        self.group_size_spin.setValue(1)
        self.volume_edit.clear()
        self.enable_volume_cb.setChecked(False)
        self.sort_combo.setCurrentIndex(0)
        self.keep_hierarchy_cb.setChecked(False)
        self.archive_rename_cb.setChecked(False)
        self.archive_suffix_edit.setText(".zip")
        self.first_rename_cb.setChecked(False)
        self.first_suffix_edit.setText(".zip")
        self.first_cb.setChecked(True)
        self.double_cb.setChecked(True)
        self.compress_format_combo.setCurrentIndex(0)
        self.keep_cb.setChecked(False)
        self.output_list_cb.setChecked(False)
        self.auto_close_cb.setChecked(True)
        self.dark_mode_cb.setChecked(False)
        self.confirm_config_cb.setChecked(True)
        self.verify_cb.setChecked(False)
        self.ext_table.setRowCount(0)
        for chk in self._ext_pickers.values():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self.naming_table.setRowCount(0)
        self._toggle_theme()
        show_styled_dialog(self, "已还原", "所有设置已恢复为默认值。", width=280, height=150)

    # ==================== 持久化 ====================

    def _password_warning(self, message):
        if message not in self._password_warnings:
            self._password_warnings.add(message)
            QTimer.singleShot(0, lambda: show_styled_dialog(self, "密码存储提示", message, width=420, height=200))

    def _save_password(self, checked=None):
        remember = self.remember_pwd_cb.isChecked()
        self.settings.setValue("remember_pwd", remember)
        if save_password(self.settings, self._credential_store, self.password_edit.text(),
                         remember, self._password_warning):
            self._saved_password = self.password_edit.text() if remember else ""

    def _save_settings(self):
        self._compression_layers = self.layer_stack.values(False)
        self.settings.setValue('unified_layers', True)
        self.settings.setValue('layer_format_preferences',json.dumps(self.layer_stack.preferences()))
        self.settings.setValue('nested_enabled', self.nested_cb.isChecked())
        self.settings.setValue('compression_layers', json.dumps(public_layers(self._compression_layers),ensure_ascii=False))
        self.settings.setValue('layer_password_required', json.dumps(self.layer_stack.password_flags()))
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("src", self.src_edit.text())
        self.settings.setValue("dest", self.dest_edit.text())
        self.settings.setValue("group_size", self._group_size_preference if getattr(self, "_format_lock_single", False) else self.group_size_spin.value())
        self.settings.setValue("sort_by", self.sort_combo.currentText())
        self.settings.setValue("remember_pwd", self.remember_pwd_cb.isChecked())
        # Unified layer passwords stay in memory; legacy credentials are read-only.
        self.settings.setValue("volume", self.volume_edit.text())
        self.settings.setValue("enable_volume", self._volume_preference if getattr(self, "_format_lock_volume", False) else self.enable_volume_cb.isChecked())
        self.settings.setValue("keep_files", self.keep_cb.isChecked())
        self.settings.setValue("recursive", self.recursive_cb.isChecked())
        self.settings.setValue("output_list", self.output_list_cb.isChecked())
        self.settings.setValue("first_compress", self.first_cb.isChecked())
        self.settings.setValue("double_compress", self.double_cb.isChecked())
        self.settings.setValue("auto_close", self.auto_close_cb.isChecked())
        self.settings.setValue("dark_mode", self.dark_mode_cb.isChecked())
        self.settings.setValue("archive_rename_enable", self.archive_rename_cb.isChecked())
        self.settings.setValue("archive_suffix", self.archive_suffix_edit.text())
        self.settings.setValue("first_rename_enable", self.first_rename_cb.isChecked())
        self.settings.setValue("first_suffix_replace", self.first_suffix_edit.text())
        self.settings.setValue("archive_format", self.compress_format_combo.currentText())
        self.settings.setValue("keep_hierarchy", self.keep_hierarchy_cb.isChecked())
        self.settings.setValue("confirm_config", self.confirm_config_cb.isChecked())
        self.settings.setValue("verify_archive", self.verify_cb.isChecked())
        self._save_ext_state()
        self._save_naming_state()

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

    def _save_naming_state(self):
        self.settings.beginWriteArray("naming_rules")
        idx = 0
        for r in range(self.naming_table.rowCount()):
            folder_item = self.naming_table.item(r, 1)
            if not folder_item or not folder_item.text().strip():
                continue
            self.settings.setArrayIndex(idx)
            chk_item = self.naming_table.item(r, 0)
            tmpl_item = self.naming_table.item(r, 2)
            self.settings.setValue("enable", chk_item.checkState() == Qt.CheckState.Checked)
            self.settings.setValue("match_folder", folder_item.text().strip())
            self.settings.setValue("template", tmpl_item.text() if tmpl_item else "")
            idx += 1
        self.settings.endArray()

    def _load_naming_state(self):
        saved = {}
        count = self.settings.beginReadArray("naming_rules")
        for r in range(count):
            self.settings.setArrayIndex(r)
            folder = self.settings.value("match_folder", "")
            if folder:
                saved[folder] = {
                    "enable": self.settings.value("enable", False, type=bool),
                    "template": self.settings.value("template", ""),
                }
        self.settings.endArray()
        self._apply_naming_sync(saved)

    def _sync_naming_from_ext(self):
        current = {}
        for r in range(self.naming_table.rowCount()):
            folder_item = self.naming_table.item(r, 1)
            if not folder_item or not folder_item.text().strip():
                continue
            chk_item = self.naming_table.item(r, 0)
            tmpl_item = self.naming_table.item(r, 2)
            current[folder_item.text().strip()] = {
                "enable": chk_item and chk_item.checkState() == Qt.CheckState.Checked,
                "template": tmpl_item.text() if tmpl_item else "",
            }
        self._apply_naming_sync(current)

    def _apply_naming_sync(self, source):
        folders = set()
        for r in range(self.ext_table.rowCount()):
            name_item = self.ext_table.item(r, 2)
            if name_item and name_item.text().strip():
                folders.add(name_item.text().strip())
        for r in range(self.naming_table.rowCount() - 1, -1, -1):
            folder_item = self.naming_table.item(r, 1)
            if folder_item and folder_item.text().strip() not in folders:
                self.naming_table.removeRow(r)

        self.naming_table.blockSignals(True)
        existing = set()
        for r in range(self.naming_table.rowCount()):
            folder_item = self.naming_table.item(r, 1)
            if folder_item and folder_item.text().strip():
                existing.add(folder_item.text().strip())

        for folder in sorted(folders):
            if folder in existing:
                continue
            data = source.get(folder, {})
            enable = data.get("enable", False)
            template = data.get("template", "{n}{ext}")
            self._add_naming_row(folder, template, enable=enable)
        self.naming_table.blockSignals(False)
        self._refresh_preview()

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

    def _del_all_ext_rows(self):
        self.ext_table.blockSignals(True)
        self.ext_table.setRowCount(0)
        self.ext_table.blockSignals(False)
        for chk in self._ext_pickers.values():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self._refresh_preview()

    def _enable_all_mappings(self):
        src_path = self.src_edit.text().strip()
        if not src_path or not os.path.isdir(src_path):
            show_styled_dialog(self, "错误", "请先选择有效的输入文件夹", width=300, height=160)
            return
        recursive = self.recursive_cb.isChecked()
        iterator = Path(src_path).rglob('*') if recursive else Path(src_path).iterdir()
        src_exts = set()
        for f in iterator:
            if f.is_file() and f.name.casefold() != RESUME_FILE_NAME:
                ext = f.suffix.lower()
                if ext:
                    src_exts.add(ext)
        if not src_exts:
            show_styled_dialog(self, "无匹配文件", "输入文件夹中未发现任何文件", width=300, height=160)
            return
        ext_to_cat = {}
        for cat_name, exts in EXT_CATEGORIES.items():
            for ext in exts:
                ext_to_cat[ext] = cat_name
        self.ext_table.blockSignals(True)
        existing = {}
        for r in range(self.ext_table.rowCount()):
            ext_item = self.ext_table.item(r, 1)
            if ext_item and ext_item.text().strip():
                existing[ext_item.text().strip()] = r
        for ext in sorted(src_exts):
            if ext in existing:
                chk_item = self.ext_table.item(existing[ext], 0)
                if chk_item:
                    chk_item.setCheckState(Qt.CheckState.Checked)
            else:
                category = ext_to_cat.get(ext)
                if category is None:
                    name, ok = QInputDialog.getText(
                        self, "自定义映射", f"扩展名 {ext} 不在预设列表中，请输入文件夹名：",
                        text=ext[1:])
                    if not ok or not name.strip():
                        continue
                    name = name.strip()
                else:
                    name = category
                self._add_ext_row(ext, name, checked=True)
        self.ext_table.blockSignals(False)
        self._sync_pickers_from_table()
        self._refresh_preview()

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
            if not chk_item or not ext_item:
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

    # ==================== 命名规则 ====================

    def _add_naming_row(self, match_folder, template, enable=True):
        row = self.naming_table.rowCount()
        self.naming_table.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked if enable else Qt.CheckState.Unchecked)
        self.naming_table.setItem(row, 0, chk)
        self.naming_table.setItem(row, 1, QTableWidgetItem(match_folder))
        self.naming_table.setItem(row, 2, QTableWidgetItem(template))

    def _del_naming_row(self):
        rows = set(i.row() for i in self.naming_table.selectedIndexes())
        for r in sorted(rows, reverse=True):
            self.naming_table.removeRow(r)
        self._refresh_preview()

    def _on_naming_item_changed(self, item):
        if item.column() == 0:
            self._refresh_preview()

    def _on_naming_cell_changed(self, row, col):
        if col in (1, 2):
            self._refresh_preview()

    def _on_naming_preset_selected(self, idx):
        if idx < 0:
            return
        tmpl, _ = RENAME_PRESETS[idx]
        row = self.naming_table.currentRow()
        if row >= 0:
            item = self.naming_table.item(row, 2)
            if item:
                self.naming_table.blockSignals(True)
                item.setText(tmpl)
                self.naming_table.blockSignals(False)
                self._refresh_preview()
        self.naming_preset_combo.setCurrentIndex(-1)

    def _on_preview_tab_changed(self, index):
        if index >= 0:
            self._refresh_preview()

    def _on_compress_format_changed(self, text):
        suffix = f".{text}"
        self.archive_suffix_edit.setText(suffix)
        self.archive_suffix_edit.setPlaceholderText(suffix)
        self.first_suffix_edit.setText(suffix)
        self.first_suffix_edit.setPlaceholderText(suffix)
        self._sync_compression_capabilities()

    def _on_first_compress_toggled(self, checked):
        if not checked and self.double_cb.isChecked():
            self.double_cb.setChecked(False)
        self._sync_compression_capabilities()

    def _on_enable_volume_toggled(self, checked):
        self._sync_compression_capabilities()

    def _on_double_compress_toggled(self, checked):
        if checked and not self.first_cb.isChecked():
            self.first_cb.setChecked(True)
        self._sync_compression_capabilities()

    def _on_nested_toggled(self, checked):
        if checked:
            self.first_cb.setChecked(True)
        self._sync_compression_capabilities()

    def _layers_changed(self):
        self._compression_layers = self.layer_stack.values(False)
        self._missing_layer_passwords = set()  # Blank layer passwords explicitly disable encryption.
        self._layer_migration_invalid = False
        self._schedule_preview()

    def _edit_layers(self):
        if self._task_busy():
            return
        dialog = LayerEditor(self._compression_layers,self)
        if self._missing_layer_passwords:
            show_styled_dialog(self,'需要重新确认密码','上次的加密层未保存密码。请在层配置中重新输入；留空并应用表示本次不加密。')
        if dialog.exec()!=QDialog.DialogCode.Accepted:
            return
        self._compression_layers = dialog.layers
        self._missing_layer_passwords = set()
        self.layers_btn.setText(f'配置压缩层（{len(dialog.layers)} 层）')
        self._schedule_preview()

    def _sync_compression_capabilities(self, *args):
        if not hasattr(self, 'compress_format_combo'):
            return
        capability = FORMATS[self.compress_format_combo.currentText()]
        nested = hasattr(self, 'nested_cb') and self.nested_cb.isChecked()
        self.first_cb.setText('启用压缩' if nested else '一次压缩')
        if hasattr(self,'layer_stack'):
            self.layer_stack.setEnabled(self.first_cb.isChecked() and not self._task_busy())
            self.add_layer_btn.setEnabled(self.first_cb.isChecked() and not self._task_busy())
        active = self.first_cb.isChecked() and not nested
        if hasattr(self, 'layers_btn'):
            self.layers_btn.setEnabled(nested and self.first_cb.isChecked())
            self.double_cb.setEnabled(not nested)
            self.compress_format_combo.setEnabled(not nested)
        self.password_group.setEnabled(active and capability['password'])
        self.password_group.setToolTip('' if capability['password'] else '此格式不支持密码；原输入保留，切回 ZIP/7Z 后恢复。')
        if not capability['volume']:
            if not getattr(self, '_format_lock_volume', False):
                self._volume_preference = self.enable_volume_cb.isChecked()
            self._format_lock_volume = True
            self.enable_volume_cb.blockSignals(True)
            self.enable_volume_cb.setChecked(False)
            self.enable_volume_cb.blockSignals(False)
        elif getattr(self, '_format_lock_volume', False):
            self._format_lock_volume = False
            self.enable_volume_cb.blockSignals(True)
            self.enable_volume_cb.setChecked(self._volume_preference)
            self.enable_volume_cb.blockSignals(False)
        self.enable_volume_cb.setEnabled(active and capability['volume'])
        self.enable_volume_cb.setToolTip('' if capability['volume'] else '此格式未开放分卷，切回 ZIP/7Z 后恢复原选择。')
        self.volume_edit.setEnabled(active and capability['volume'] and self.enable_volume_cb.isChecked())
        if not capability['multiple']:
            if not getattr(self, '_format_lock_single', False):
                self._group_size_preference = self.group_size_spin.value()
            self._format_lock_single = True
            self.group_size_spin.blockSignals(True)
            self.group_size_spin.setMaximum(1)
            self.group_size_spin.blockSignals(False)
        elif getattr(self, '_format_lock_single', False):
            self._format_lock_single = False
            self.group_size_spin.blockSignals(True)
            self.group_size_spin.setMaximum(9999)
            self.group_size_spin.setValue(self._group_size_preference)
            self.group_size_spin.blockSignals(False)
        self.group_size_spin.setEnabled(active and capability['multiple'])
        self.group_size_spin.setToolTip('' if capability['multiple'] else 'GZ/XZ 每包只支持一个文件，已锁定为 1。')
        first_rename = active and not self.enable_volume_cb.isChecked()
        self.first_rename_cb.setEnabled(first_rename)
        self.first_suffix_edit.setEnabled(first_rename and self.first_rename_cb.isChecked())
        second_rename = active and self.double_cb.isChecked()
        self.archive_rename_cb.setEnabled(second_rename)
        self.archive_suffix_edit.setEnabled(second_rename and self.archive_rename_cb.isChecked())
        self.verify_cb.setEnabled(active)
        self._schedule_preview()

    def _schedule_preview(self, *args):
        if not self._task_busy():
            self._preview_timer.start()

    def _refresh_preview(self):
        self._preview_timer.stop()
        if not hasattr(self, 'compress_format_combo'):
            return
        config = self._build_config()
        if not config['src'] or not os.path.isdir(config['src']):
            plan = []
        else:
            config['dest'] = config['dest'] or str(Path(config['src']).parent / 'SortZip_Preview_Output')
            try:
                plan = (prepare_config(config)['execution_plan'] if config.get('compression_layers') is not None
                        else build_execution_plan(config))
            except (ValueError, OSError) as error:
                self.preview_table.setRowCount(0)
                self.preview_table.setToolTip(str(error))
                self.preview_status.setText(str(error))
                return
        self.preview_table.setToolTip('')
        self.preview_status.setText('')
        folders = sorted({entry['folder'] for entry in plan})
        self._preview_order = {key: value for key, value in self._preview_order.items() if key in folders}
        current = self.preview_tab_bar.tabText(self.preview_tab_bar.currentIndex())
        self.preview_tab_bar.blockSignals(True)
        while self.preview_tab_bar.count():
            self.preview_tab_bar.removeTab(0)
        for folder in folders:
            self.preview_tab_bar.addTab(folder)
        if folders:
            self.preview_tab_bar.setCurrentIndex(folders.index(current) if current in folders else 0)
        self.preview_tab_bar.blockSignals(False)
        selected = self.preview_tab_bar.tabText(self.preview_tab_bar.currentIndex())
        self.preview_table.setRowCount(0)
        for entry in plan:
            if entry['folder'] != selected:
                continue
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            source = QTableWidgetItem(entry['source_rel'])
            source.setData(Qt.ItemDataRole.UserRole, entry['source_rel'])
            self.preview_table.setItem(row, 0, source)
            self.preview_table.setItem(row, 1, QTableWidgetItem(entry['target_name']))
            self.preview_table.setItem(row, 2, QTableWidgetItem(entry['archive']))

    def _on_preview_reordered(self):
        selected = self.preview_tab_bar.tabText(self.preview_tab_bar.currentIndex())
        if not selected:
            return
        self._preview_order[selected] = [self.preview_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                                        for row in range(self.preview_table.rowCount())
                                        if self.preview_table.item(row, 0)]
        QTimer.singleShot(0, self._refresh_preview)

    def _collect_naming_rules(self):
        rules = []
        for r in range(self.naming_table.rowCount()):
            chk_item = self.naming_table.item(r, 0)
            folder_item = self.naming_table.item(r, 1)
            tmpl_item = self.naming_table.item(r, 2)
            if folder_item and folder_item.text().strip():
                rules.append({
                    "enable": chk_item and chk_item.checkState() == Qt.CheckState.Checked,
                    "match_folder": folder_item.text().strip(),
                    "template": tmpl_item.text().strip() if tmpl_item else "",
                })
        return rules

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

        config = {
            'src': self.src_edit.text().strip(),
            'dest': self.dest_edit.text().strip(),
            'group_size': self.group_size_spin.value(),
            'password': self.password_edit.text() if self.password_group.isEnabled() else '',
            'volume': vol if self.enable_volume_cb.isChecked() else None,
            'enable_volume': self.enable_volume_cb.isChecked(),
            'bandizip': 'bandizip',
            'custom_names': custom_names,
            'sort_by': SORT_MAP.get(self.sort_combo.currentText(), 'name'),
            'keep_files': self.keep_cb.isChecked(),
            'recursive': self.recursive_cb.isChecked(),
            'output_list': self.output_list_cb.isChecked(),
            'first_compress': self.first_cb.isChecked(),
            'double_compress': self.double_cb.isChecked(),
            'auto_close': self.auto_close_cb.isChecked(),
            'naming_rules': self._collect_naming_rules(),
            'archive_suffix': self.archive_suffix_edit.text().strip() if self.double_cb.isChecked() and self.archive_rename_cb.isChecked() else '.' + self.compress_format_combo.currentText(),
            'first_suffix_replace': self.first_suffix_edit.text().strip()
                            if (self.first_cb.isChecked() and self.first_rename_cb.isChecked() and not self.enable_volume_cb.isChecked()) else None,
            'archive_format': self.compress_format_combo.currentText(),
            'keep_hierarchy': self.keep_hierarchy_cb.isChecked(),
            'verify_archive': self.verify_cb.isChecked(),
            'preview_order': dict(self._preview_order),
        }
        if hasattr(self,'nested_cb') and self.nested_cb.isChecked() and self.first_cb.isChecked():
            config['compression_layers'] = self.layer_stack.values(False)
        return config

    def _confirm_config(self, config):
        vol_text = config.get('volume') or '自动'
        vol_enabled = "✔ 启用" if config.get('enable_volume', False) else "✘ 禁用"
        hier = "✔ 开启" if config.get('keep_hierarchy', False) else "✘ 关闭"
        keep = "✔ 保留" if config.get('keep_files', False) else "✘ 不保留"
        out = "✔ 生成" if config.get('output_list', False) else "✘ 不生成"
        ver = "✔ 开启" if config.get('verify_archive', False) else "✘ 关闭"

        fc = config.get('first_compress', True)
        dc = config.get('double_compress', True)
        if not fc:
            mode = "不压缩"
        elif dc:
            mode = "一次压缩 + 二次压缩"
        else:
            mode = "仅一次压缩"

        lines = [
            ("每包文件数", f"{config.get('group_size', 1)} 个"),
            ("压缩模式", mode),
            ("真实压缩格式", config.get("archive_format", "zip").upper()),
            ("启用分卷", f"{vol_enabled}（{vol_text}）"),
            ("保持原文件夹层级", hier),
            ("保留原始文件", keep),
            ("输出 List.txt", out),
            ("压缩后校验完整性", ver),
        ]
        if config.get('compression_layers'):
            layers = config['compression_layers']
            lines = [('压缩模式',f'多层压缩，共 {len(layers)} 层')] + [
                (f'第 {i+1} 层',f'{layer.archive_format.upper()} / 每组 {layer.group_size} 单元 / '
                 f'{"分卷" if layer.enable_volume else "不分卷"} / '
                 f'{"加密" if layer.password else "不加密"} / '
                 f'{"校验" if layer.verify else "不校验"}') for i,layer in enumerate(layers)] + [
                ('保持原文件夹层级',hier),('保留原始文件',keep),('输出 List.txt',out),
                ('最外层后缀',layers[-1].suffix or '.'+layers[-1].archive_format)]

        # 文件包含情况检查
        custom_names = config.get('custom_names', {})
        src = config.get('src', '')
        include_info = None
        if custom_names and src and os.path.isdir(src):
            recursive = config.get('recursive', False)
            iterator = Path(src).rglob('*') if recursive else Path(src).iterdir()
            all_files = [f for f in iterator if f.is_file()]
            total = len(all_files)
            checked_exts = set(custom_names.keys())
            excluded = [f for f in all_files if f.suffix.lower() not in checked_exts]
            excluded_count = len(excluded)
            if excluded_count == 0:
                include_info = f"✔ 已包含文件夹内全部 {total} 个文件"
            else:
                sample = excluded[:5]
                names = "、".join(f.name for f in sample)
                suffix = "…" if excluded_count > 5 else ""
                include_info = f"⚠ 已勾选扩展名未覆盖全部文件（{total} 个中 {excluded_count} 个未匹配）\n例如：{names}{suffix}"

        dlg = QDialog(self)
        dlg.setWindowTitle("参数确认")
        dlg.setMinimumSize(460, 450)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(8)

        title = QLabel("请确认以下执行参数：")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(title)
        layout.addSpacing(4)

        form = QFormLayout()
        form.setSpacing(6)
        form.setContentsMargins(12, 0, 12, 0)
        for label, value in lines:
            v = QLabel(value)
            v.setWordWrap(True)
            v.setStyleSheet("color: #0078d4; font-weight: bold;")
            form.addRow(QLabel(label + "："), v)
        if config.get('compression_layers'):
            content = QWidget()
            content.setLayout(form)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            scroll.setMinimumHeight(240)
            scroll.setMaximumHeight(340)
            layout.addWidget(scroll)
        else:
            layout.addLayout(form)

        if include_info:
            layout.addSpacing(4)
            info_label = QLabel(include_info)
            info_label.setWordWrap(True)
            info_label.setStyleSheet(
                "background: #2d2d2d; color: #ffcc00; padding: 8px 12px;"
                " border-radius: 4px; font-size: 12px;"
            )
            layout.addWidget(info_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn = QPushButton("确认执行")
        ok_btn.setStyleSheet("background: #0078d4; color: white; padding: 4px 16px;")
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _task_busy(self):
        return self.thread is not None or self.worker is not None or self._close_pending

    def _set_task_active(self, active):
        if active:
            self._preview_timer.stop()
        for index in (0, 1, 2):
            self.stack.widget(index).setEnabled(not active)
        for widget in (self.first_cb, self.double_cb, self.compress_format_combo,
                       self.keep_cb, self.output_list_cb, self.auto_close_cb, self.verify_cb, self.reset_btn,
                       self.nested_cb, self.layers_btn):
            widget.setEnabled(not active)
        self.layer_stack.setEnabled(not active and self.first_cb.isChecked())
        self.add_layer_btn.setEnabled(not active and self.first_cb.isChecked())
        self.run_btn.setEnabled(not active)
        self.resume_btn.setEnabled(not active)
        self.cancel_btn.setEnabled(active)
        if not active:
            self._sync_compression_capabilities()

    def _run(self):
        if self._task_busy():
            self._append_log('任务仍在运行或退出中，请等待完成。\n')
            return
        if self.first_cb.isChecked():
            if self._layer_migration_invalid:
                show_styled_dialog(self,'请确认压缩层','旧设置迁移失败，请在卡片中调整配置后再执行。')
                return
            try:
                self._compression_layers = self.layer_stack.values()
                plan_layers((),self._compression_layers)
                self._missing_layer_passwords = set()
            except ValueError as error:
                show_styled_dialog(self,'层参数无效',str(error))
                return
        config = self._build_config()
        if config.get('compression_layers') and self._missing_layer_passwords:
            show_styled_dialog(self,'请重新确认各层密码','上次有压缩层使用密码。请打开「配置压缩层」重新输入或明确清空后应用，避免无意取消加密。')
            return

        try:
            config = prepare_config(config,
                confirmation=self.password_confirm_edit.text() if self.password_group.isEnabled() else None,
                require_files=True)
        except (ValueError, OSError) as error:
            show_styled_dialog(self, getattr(error, 'title', '参数或执行计划无效'), str(error), width=460, height=220)
            return

        if self.confirm_config_cb.isChecked():
            if not self._confirm_config(config):
                self.run_btn.setEnabled(True)
                return

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self._last_dest_path = config.get('dest', '')
        self._launch_worker(config)

    def _cancel(self):
        if self.worker and self._pending_result is None and self.thread is not None and self.thread.isRunning():
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
        self._append_log("正在取消，将在当前压缩阶段完成并记录断点后停止...")

    @Slot()
    def _on_cancel_waiting(self):
        if not self.worker or getattr(self, '_cancel_dlg', None):
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("正在停止")
        dlg.setFixedSize(340, 170)
        layout = QVBoxLayout(dlg)
        title_lbl = QLabel("取消已排队")
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_lbl)
        msg_lbl = QLabel("当前压缩阶段完成并记录断点后将自动停止，请稍候…")
        msg_lbl.setWordWrap(True)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(msg_lbl)
        layout.addStretch()
        force_btn = QPushButton("强制取消")
        force_btn.setStyleSheet("background: #c00000; color: white; padding: 6px 16px;")
        force_btn.clicked.connect(lambda: self._force_cancel(dlg))
        layout.addWidget(force_btn)
        self._cancel_dlg = dlg
        dlg.finished.connect(lambda *_: setattr(self, '_cancel_dlg', None))
        dlg.show()

    def _force_cancel(self, dlg):
        dlg.accept()
        if self.worker:
            self.worker.force_cancel()
            self._append_log("已请求强制取消（当前压缩将立即终止）")

    def _import_resume(self):
        if self.worker:
            show_styled_dialog(self, "任务进行中", "请先等待当前任务结束")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择断点文件", "", f"断点文件 ({RESUME_FILE_NAME})")
        if not path:
            return
        self._start_resume(path)

    def _start_resume(self, checkpoint_path):
        if self._task_busy():
            self._append_log('任务仍在运行或退出中，暂不能恢复其他任务。\n')
            return
        try:
            header = json.loads(Path(checkpoint_path).read_text(encoding='utf-8'))
        except (ValueError,OSError):
            header = None
        if isinstance(header,dict) and header.get('kind')=='multilayer':
            self._start_nested_resume(checkpoint_path,header)
            return
        try:
            data = _load_checkpoint(checkpoint_path)
        except ValueError as e:
            show_styled_dialog(self, "断点文件无效", str(e))
            return
        groups = data.get('groups', [])
        cfg = data.get('config', {})
        if not groups:
            show_styled_dialog(self, "断点文件无效", "断点文件中没有分组信息")
            return
        pending = sum(1 for g in groups if g.get('state') != 'final_done')
        if pending <= 0:
            dlg = QDialog(self)
            dlg.setWindowTitle("任务已完成")
            layout = QVBoxLayout(dlg)
            lbl = QLabel("该断点文件中的所有分组均已完成，无需恢复。\n是否删除该断点文件？")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
            btn_row = QHBoxLayout()
            cancel_btn = QPushButton("保留")
            cancel_btn.clicked.connect(dlg.reject)
            ok_btn = QPushButton("删除")
            ok_btn.clicked.connect(dlg.accept)
            btn_row.addWidget(cancel_btn)
            btn_row.addWidget(ok_btn)
            layout.addLayout(btn_row)
            dlg.setFixedSize(300, 140)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                try:
                    Path(checkpoint_path).unlink()
                    self._append_log(f"已删除断点文件: {checkpoint_path}\n")
                except OSError as e:
                    self._append_log(f"删除断点文件失败: {e}\n")
            return

        password = ""
        if self.settings.value("remember_pwd", False, type=bool):
            password = self._saved_password
        problems = validate_resume_files(checkpoint_path)
        if problems:
            shown = problems[:8]
            extra = len(problems) - len(shown)
            msg = ("检测到断点引用的源文件存在问题：\n\n"
                   + "\n".join("• " + p for p in shown))
            if extra > 0:
                msg += f"\n… 等共 {len(problems)} 项"
            msg += ("\n\n继续恢复将跳过存在问题的分组，已完成的压缩包不受影响。\n"
                    "如需放弃恢复，请点击「取消恢复」。")
            dlg = QDialog(self)
            dlg.setWindowTitle("检测到文件异常")
            layout = QVBoxLayout(dlg)
            title_lbl = QLabel("检测到文件异常")
            title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
            title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title_lbl)
            msg_lbl = QLabel(msg)
            msg_lbl.setWordWrap(True)
            msg_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(msg_lbl)
            btn_row = QHBoxLayout()
            cancel_btn = QPushButton("取消恢复")
            cancel_btn.clicked.connect(dlg.reject)
            continue_btn = QPushButton("仍然继续")
            continue_btn.setStyleSheet("background: #0078d4; color: white; padding: 4px 16px;")
            continue_btn.clicked.connect(dlg.accept)
            btn_row.addWidget(cancel_btn)
            btn_row.addWidget(continue_btn)
            layout.addLayout(btn_row)
            dlg.setFixedSize(460, 340)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
        if not self._confirm_resume(checkpoint_path, cfg, groups, pending):
            return
        hint = "原任务已设置密码" if cfg.get('password') else "原任务无密码，可留空"
        dlg = PasswordDialog(self, initial=password, hint=hint)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        password = dlg.password()

        config = {
            'resume_from': checkpoint_path,
            'password': password,
            'bandizip': 'bandizip',
            'dest': str(Path(checkpoint_path).parent),
        }
        self._last_dest_path = config['dest']
        self._launch_worker(config)

    def _start_nested_resume(self, checkpoint_path, header):
        from sortzip_core.nested_resume import load_resume
        # Validate structure and files before requesting any actual passwords.
        try:
            rows = header.get('tasks', [])
            placeholders = {row['layer']:'validation-only' for row in rows
                if isinstance(row,dict) and type(row.get('layer')) is int
                and isinstance(row.get('settings'),dict) and row['settings'].get('password_required')}
            plan,_,state = load_resume(checkpoint_path,passwords=placeholders)
        except (ValueError,OSError,TypeError,KeyError) as error:
            show_styled_dialog(self,'多层断点无效',str(error),width=460,height=240)
            return
        passwords = {}
        for level in sorted(placeholders):
            dialog = PasswordDialog(self,initial=self._saved_password,
                hint=f'请提供第 {level+1} 层原任务密码；各层可能不同。')
            if dialog.exec()!=QDialog.DialogCode.Accepted:
                return
            passwords[level] = dialog.password()
        try:
            plan,_,state = load_resume(checkpoint_path,passwords=passwords)
        except (ValueError,OSError) as error:
            show_styled_dialog(self,'恢复参数无效',str(error))
            return
        if not self._confirm_nested_resume(len(plan.tasks),len(state['completed']),
                                           max(t.layer_index for t in plan.tasks)+1):
            return
        self._last_dest_path = str(Path(checkpoint_path).resolve().parent)
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self._launch_worker(dict(resume_from=str(checkpoint_path),layer_passwords=passwords,
                                 bandizip='bandizip'))

    def _confirm_nested_resume(self, total, done, levels):
        dialog = QDialog(self)
        dialog.setWindowTitle('恢复多层压缩任务')
        layout = QVBoxLayout(dialog)
        label = QLabel(f'共 {levels} 层、{total} 组，已完成 {done} 组，剩余 {total-done} 组。\n'
                       '将复用已完成产物；中断半成品保留，剩余组写入新重试目录。')
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QHBoxLayout()
        cancel = QPushButton('取消')
        cancel.clicked.connect(dialog.reject)
        start = QPushButton('恢复任务' if done<total else '核对已完成任务')
        start.clicked.connect(dialog.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(start)
        layout.addLayout(buttons)
        dialog.setMinimumWidth(420)
        return dialog.exec()==QDialog.DialogCode.Accepted

    def _confirm_resume(self, checkpoint_path, cfg, groups, pending):
        volume = cfg.get('volume')
        msg = (f"断点文件: {Path(checkpoint_path).name}\n"
               f"输出文件夹: {Path(checkpoint_path).parent}\n"
               f"待处理组数: {pending} / {len(groups)}\n"
               f"分组大小: {cfg.get('group_size')}\n"
               f"压缩格式: {cfg.get('archive_format', 'zip').upper()}\n"
               f"分卷: {volume or '自动检测'}\n"
               f"后缀: {cfg.get('archive_suffix', '.zipp')}\n"
               f"二次打包: {'开启' if cfg.get('double_compress', True) else '关闭'}\n"
               f"校验完整性: {'开启' if cfg.get('verify', False) else '关闭'}\n\n"
               f"恢复将以断点文件中的配置为准。")
        dlg = QDialog(self)
        dlg.setWindowTitle("确认恢复")
        layout = QVBoxLayout(dlg)
        title_lbl = QLabel("恢复确认")
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_lbl)
        msg_lbl = QLabel(msg)
        msg_lbl.setWordWrap(True)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(msg_lbl)
        btn_row = QHBoxLayout()
        cbtn = QPushButton("取消")
        cbtn.clicked.connect(dlg.reject)
        obtn = QPushButton("确认恢复")
        obtn.setStyleSheet("background: #0078d4; color: white; padding: 4px 16px;")
        obtn.clicked.connect(dlg.accept)
        btn_row.addWidget(cbtn)
        btn_row.addWidget(obtn)
        layout.addLayout(btn_row)
        dlg.setFixedSize(420, 320)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _launch_worker(self, config):
        if self._task_busy():
            self._append_log('已有任务，已阻止重复启动。\n')
            return False
        self._pending_result = None
        self._set_task_active(True)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        try:
            self.thread, self.worker = create_worker_thread(config)
        except Exception:
            self.thread = None
            self.worker = None
            self._set_task_active(False)
            self._append_log('无法创建后台任务，已恢复界面；请检查运行环境后重试。\n')
            return False
        self.worker.finished.connect(self._save_settings)
        self.worker.finished.connect(self._on_finished)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.worker.log.connect(self._append_log)
        self.worker.error.connect(lambda e: self._append_log(f"[错误] {e}"))
        self.worker.progress.connect(self._update_progress)
        self.worker.cancel_waiting.connect(self._on_cancel_waiting)
        self.thread.start()

    @Slot(int, int, str)
    def _update_progress(self, value, maximum, text):
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(f"{text}  [{value}%]")

    def _on_finished(self):
        self.cancel_btn.setEnabled(False)
        if getattr(self, '_cancel_dlg', None):
            self._cancel_dlg.accept()
        stats = self.worker.stats if self.worker else {}
        status = self.worker.status if self.worker else "completed"
        if status == "completed":
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("完成  [100%]")
        else:
            labels = {"error": "处理失败", "safe_cancelled": "已安全中断", "force_cancelled": "已强制中断", "partial": "部分分组跳过，保留断点"}
            self.progress_bar.setFormat(labels.get(status, "任务已停止"))
        self._pending_result = (dict(stats), status, getattr(self, '_last_dest_path', ''))

    def _on_thread_finished(self):
        result = self._pending_result
        self._pending_result = None
        self.thread = None
        self.worker = None
        if self._close_pending:
            QTimer.singleShot(0, self.close)
            return
        self._set_task_active(False)
        if result is not None:
            stats, status, dest_path = result
            show_stats_dialog(self, stats, dest_path=dest_path, status=status)

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
        if self.thread is not None or self.worker is not None:
            event.ignore()
            if not self._close_pending:
                self._close_pending = True
                self._append_log('已请求关闭，将在安全停止并释放工作线程后退出。\n')
                if self._pending_result is None and self.worker is not None:
                    self._cancel()
            return
        self._save_settings()
        checker = getattr(self, '_update_checker', None)
        if checker is not None and checker.isRunning():
            checker.requestInterruption()
            checker.wait(6000)
        super().closeEvent(event)
