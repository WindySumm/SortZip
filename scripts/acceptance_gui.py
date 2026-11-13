r"""Visible acceptance sandbox: generated files, isolated settings, no real credentials.

Run from the repository: .venv\Scripts\pythonw.exe scripts\acceptance_gui.py
"""
import json
import argparse
import sys
import time
import uuid
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from sortzip_core.app import MainWindow
from sortzip_core.smoke import _MemoryCredentials


def positive(value):
    number = int(value)
    if number<1: raise argparse.ArgumentTypeError('must be positive')
    return number


def nonnegative(value):
    number = float(value)
    if number<0: raise argparse.ArgumentTypeError('must not be negative')
    return number


def main():
    parser = argparse.ArgumentParser(description='Isolated visible acceptance sandbox')
    parser.add_argument('--slow-compression', action='store_true',
                        help='Inject a 10-second wait before compression, only to reproduce close/cancel safely')
    parser.add_argument('--compression-delay',type=nonnegative,default=10)
    parser.add_argument('--preview-delay',type=nonnegative,default=0,
                        help='Cooperative test-host wait before preview planning, for close/lifecycle checks')
    parser.add_argument('--compress',action='store_true',help='Enable the real two-layer acceptance task')
    parser.add_argument('--volume',action='store_true',help='Generate deterministic binary payloads and ZIP volumes')
    parser.add_argument('--payload-kib',type=positive,default=4)
    parser.add_argument('--first-format',choices=('zip','7z'),default='zip')
    parser.add_argument('--group-size',type=positive,default=4)
    parser.add_argument('--width',type=positive,default=1280)
    parser.add_argument('--height',type=positive,default=800)
    parser.add_argument('--dark',action='store_true')
    parser.add_argument('--native-caption',action='store_true',help='Keep the production caption and record per-window DWM state')
    parser.add_argument('--resume',type=str,default='')
    options = parser.parse_args()
    root = ROOT/'.build-validation'/'acceptance'/('gui-' + uuid.uuid4().hex[:8])
    source = root/'source'
    (source/'子目录').mkdir(parents=True)
    generated = random.Random(927)
    for index in range(1, 13):
        if options.volume or options.payload_kib!=4:
            (source/f'验收_{index}.txt').write_bytes(generated.randbytes(max(1,options.payload_kib)*1024))
        else:
            (source/f'验收_{index}.txt').write_text(f'Generated acceptance file {index}\n' * 40, encoding='utf-8')
    (source/'子目录'/'验收_1.txt').write_text('Nested duplicate basename', encoding='utf-8')
    settings = QSettings(str(root/'settings.ini'), QSettings.Format.IniFormat)
    for key, value in dict(src=str(source), dest=str(root/'output-one'), keep_files=True,dark_mode=options.dark,
            recursive=True, keep_hierarchy=True, first_compress=options.slow_compression or options.compress,
            verify_archive=options.slow_compression or options.compress, double_compress=False,
            group_size=options.group_size, output_list=True, confirm_config=False).items():
        settings.setValue(key, value)
    settings.beginWriteArray('ext_mappings')
    settings.setArrayIndex(0)
    for key, value in dict(checked=True, ext='.txt', name='文档').items():
        settings.setValue(key, value)
    settings.endArray()
    settings.beginWriteArray('naming_rules')
    settings.setArrayIndex(0)
    for key, value in dict(enable=True, match_folder='文档', template='{folder}_{n}{ext}').items():
        settings.setValue(key, value)
    settings.endArray()
    records = []
    if options.slow_compression or options.compress or options.resume:
        from sortzip_core import engine
        run_process = engine._run_proc
        def slow_process(cmd, force_cancel_check=None, wait_ms=200, **options):
            if options.slow_compression and len(cmd) > 1 and cmd[1] == 'a':
                until = time.monotonic() + max(0,options.compression_delay)
                while time.monotonic() < until:
                    if force_cancel_check and force_cancel_check():
                        raise engine.ForceCancelled()
                    time.sleep(0.05)
            events = root/'process-events.jsonl'
            with events.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(operation=cmd[1],event='real_started',at=time.time()))+'\n')
            try:
                result = run_process(cmd, force_cancel_check, wait_ms, **options)
            except Exception as error:
                with events.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(dict(operation=cmd[1],event=type(error).__name__,at=time.time()))+'\n')
                raise
            with events.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(operation=cmd[1],event='real_finished',at=time.time()))+'\n')
            return result
        engine._run_proc = slow_process
    class AcceptanceWindow(MainWindow):
        def _task_finished(self,stats,status,destination):
            records.append(dict(status=status, stats=dict(stats),output=destination))
            (root/'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
            super()._task_finished(stats,status,destination)
    app = QApplication([sys.argv[0]])
    window = AcceptanceWindow(settings=settings, credential_store=_MemoryCredentials(), check_updates=False)
    if not options.native_caption:
        window.setWindowTitle(f'SortZip — 工作台验收 {options.width}×{options.height} · {root.name}' + ('（慢任务）' if options.slow_compression else ''))
    if options.slow_compression or options.compress:
        window.navigate(window.state.layers[0].key)
        window.layer_editor.format.setCurrentText(options.first_format)
        if options.volume:
            window.layer_editor.volume.setChecked(True)
            window.layer_editor.size.setText('1k')
        window.add_layer()
        window.layer_editor.group.setValue(2)
        window.layer_editor.format.setCurrentText('7z')
        window.layer_editor.rename.setChecked(True)
        window.layer_editor.suffix.setText('.png')
    window.resize(options.width,options.height)
    window.show()
    if options.native_caption:
        import ctypes
        from ctypes import wintypes
        from PySide6.QtCore import QTimer
        from dataclasses import asdict
        from PySide6.QtGui import QKeySequence,QShortcut
        window.theme_btn.setShortcut(QKeySequence('Ctrl+Alt+T'))
        window.addAction(window.theme_btn)
        def recreate_native_handle():
            window.hide()
            window.destroy()
            window.create()
            window.show()
        recreate_shortcut = QShortcut(QKeySequence('Ctrl+Alt+R'),window)
        recreate_shortcut.activated.connect(recreate_native_handle)
        getter = ctypes.WinDLL('dwmapi').DwmGetWindowAttribute
        getter.argtypes = [wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        getter.restype = ctypes.c_int32
        dpi_getter = ctypes.WinDLL('user32').GetDpiForWindow
        dpi_getter.argtypes = [wintypes.HWND]
        dpi_getter.restype = wintypes.UINT
        last_snapshot = None
        def record_caption():
            nonlocal last_snapshot
            if window._closing: return
            hwnd = int(window.effectiveWinId())
            attributes = {}
            for attribute in (20,34,35,36):
                value = ctypes.c_uint32()
                hr = getter(wintypes.HWND(hwnd),attribute,ctypes.byref(value),ctypes.sizeof(value))
                attributes[str(attribute)] = dict(value=value.value if hr>=0 else None,hresult=f'0x{hr & 0xffffffff:08X}')
            def rectangle(rect): return [rect.x(),rect.y(),rect.width(),rect.height()]
            snapshot = dict(hwnd=hwnd,title=window.windowTitle(),icon_present=not window.windowIcon().isNull(),
                dark=window._dark,system_color_scheme=app.styleHints().colorScheme().name,
                maximized=window.isMaximized(),minimized=window.isMinimized(),
                geometry=rectangle(window.geometry()),frame=rectangle(window.frameGeometry()),
                available=rectangle(window.screen().availableGeometry()),
                dpi_scale=window.devicePixelRatioF(),native_dpi=dpi_getter(wintypes.HWND(hwnd)),attributes=attributes,
                status=asdict(window.native_caption.status) if window.native_caption.status else None)
            if snapshot!=last_snapshot:
                last_snapshot = snapshot
                (root/'titlebar-state.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
                with (root/'titlebar-history.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(snapshot,ensure_ascii=False)+'\n')
        caption_timer = QTimer(window)
        caption_timer.timeout.connect(record_caption)
        caption_timer.start(500)
    if options.preview_delay:
        from PySide6.QtCore import QTimer
        from sortzip_core.workspace_planning import build_workspace_preview
        submit = window.preview_service.submit
        def delayed_preview(config,revision,cancelled,strict):
            until = time.monotonic()+options.preview_delay
            while time.monotonic()<until:
                if cancelled(): return None
                time.sleep(.01)
            return build_workspace_preview(config,revision,cancelled,strict)
        window.preview_service.submit = lambda config,revision:submit(config,revision,builder=delayed_preview)
        window.preview_coordinator.invalidate()
        window.state.revision += 1
        QTimer.singleShot(0,window.refresh)
    (root.parent/'current-gui.json').write_text(json.dumps(dict(root=str(root), source=str(source)), ensure_ascii=False), encoding='utf-8')
    if options.resume:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(500,lambda:window._start_resume(options.resume))
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
