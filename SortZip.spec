# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PySide6.QtCore import QLibraryInfo

qt_translations = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/sortzip.ico', 'assets'), ('sortzip_core/restore_names.ps1', 'sortzip_core'),
           ('sortzip_core/restore_category.ps1', 'sortzip_core'),
           ('sortzip_core/translations/en.json', 'sortzip_core/translations'),
           (str(qt_translations / 'qtbase_zh_CN.qm'), 'PySide6/translations')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SortZip',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/sortzip.ico'],
)
