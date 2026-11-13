"""Optional per-window DWM colors; never replace the native Windows frame."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass,replace
import sys

from PySide6.QtCore import QObject,QEvent,QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication


COLOR_DEFAULT = 0xffffffff
DARK_MODE, BORDER_COLOR, CAPTION_COLOR, TEXT_COLOR = 20,34,35,36


def colorref(color):
    value = QColor(color)
    if not value.isValid(): raise ValueError('Invalid caption color')
    return value.red() | value.green()<<8 | value.blue()<<16


@dataclass(frozen=True)
class CaptionStatus:
    dark_mode: bool = False
    custom_colors: bool = False
    errors: tuple = ()


class CaptionIdentityApi:
    """Suppress caption decoration without clearing HWND text or icon handles."""
    class Options(ctypes.Structure):
        _fields_ = [('flags',wintypes.DWORD),('mask',wintypes.DWORD)]

    def __init__(self,theme=None,user=None):
        self.theme = theme if theme is not None else ctypes.WinDLL('uxtheme',use_last_error=True)
        self.user = user if user is not None else ctypes.WinDLL('user32',use_last_error=True)
        self.theme.SetWindowThemeAttribute.argtypes = [wintypes.HWND,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.theme.SetWindowThemeAttribute.restype = ctypes.c_int32
        self.user.GetWindowLongW.argtypes = [wintypes.HWND,ctypes.c_int]
        self.user.GetWindowLongW.restype = ctypes.c_int32
        self.user.SetWindowLongW.argtypes = [wintypes.HWND,ctypes.c_int,ctypes.c_int32]
        self.user.SetWindowLongW.restype = ctypes.c_int32
        self.user.SetWindowPos.argtypes = [wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        self.user.SetWindowPos.restype = wintypes.BOOL
        self.user.SendMessageW.argtypes = [wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t]
        self.user.SendMessageW.restype = ctypes.c_ssize_t

    def hide(self,hwnd):
        hwnd = wintypes.HWND(hwnd)
        options = self.Options(3,3)  # WTNCA_NODRAWCAPTION | WTNCA_NODRAWICON
        result = self.theme.SetWindowThemeAttribute(hwnd,1,ctypes.byref(options),ctypes.sizeof(options))
        if result<0: return (f'caption:0x{result & 0xffffffff:08X}',)
        # Modern DWM may ignore NODRAWICON. A native dialog-frame decoration
        # suppresses only the caption icon; keep thick frame, system menu and
        # APPWINDOW/TOOLWINDOW bits intact. This does not make the window modal.
        ctypes.set_last_error(0)
        style = self.user.GetWindowLongW(hwnd,-20)
        if not style and ctypes.get_last_error(): return ('caption:GetWindowLongW',)
        self.user.SendMessageW(hwnd,0x80,0,0)  # WM_SETICON / ICON_SMALL; ICON_BIG stays intact.
        if not style & 1:
            ctypes.set_last_error(0)
            previous = self.user.SetWindowLongW(hwnd,-20,style | 1)
            if not previous and ctypes.get_last_error(): return ('caption:SetWindowLongW',)
            if not self.user.SetWindowPos(hwnd,None,0,0,0,0,0x37):
                self.user.SetWindowLongW(hwnd,-20,style)
                return ('caption:SetWindowPos',)
        return ()


class DwmCaptionApi:
    def __init__(self,*,library=None,build=None):
        self.build = sys.getwindowsversion().build if build is None else build
        self.library = library if library is not None else ctypes.WinDLL('dwmapi',use_last_error=True)
        self.setter = self.library.DwmSetWindowAttribute
        self.setter.argtypes = [wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        self.setter.restype = ctypes.c_int32

    def apply(self,hwnd,dark,background,foreground):
        errors = []
        def set_attribute(attribute,value,kind=ctypes.c_uint32):
            data = kind(value)
            try:
                result = self.setter(wintypes.HWND(hwnd),attribute,ctypes.byref(data),ctypes.sizeof(data))
                if result<0:
                    errors.append(f'{attribute}:0x{result & 0xffffffff:08X}')
                    return False
                return True
            except (OSError,ValueError) as error:
                errors.append(f'{attribute}:{type(error).__name__}')
                return False
        dark_ok = set_attribute(DARK_MODE,int(dark),ctypes.c_int32)
        custom = False
        if self.build>=22000:
            background_ok = set_attribute(CAPTION_COLOR,colorref(background))
            foreground_ok = set_attribute(TEXT_COLOR,colorref(foreground))
            custom = background_ok and foreground_ok
            if custom:
                if not set_attribute(BORDER_COLOR,colorref(background)):
                    set_attribute(BORDER_COLOR,COLOR_DEFAULT)
            else:
                # An incomplete pair could produce unreadable caption text. Restore
                # the OS colors as a pair, retaining its ordinary native contrast.
                for attribute in (CAPTION_COLOR,TEXT_COLOR,BORDER_COLOR):
                    set_attribute(attribute,COLOR_DEFAULT)
        return CaptionStatus(bool(dark) and dark_ok,custom,tuple(dict.fromkeys(errors)))


class NativeCaption(QObject):
    """Coalesced updates across theme changes and native handle recreation."""
    def __init__(self,window,log):
        super().__init__(window)
        self.window,self.log = window,log
        self.backend = None
        self.identity_backend = None
        self.status = None
        self.theme = None
        self._last = None
        self._reported = set()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.sync)
        window.installEventFilter(self)

    def set_theme(self,dark,background,foreground):
        self.theme = (bool(dark),background,foreground)
        self.timer.start(0)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.WinIdChange:
            self._last = None
            self.timer.start(0)
        elif event.type() in (QEvent.Type.Show,QEvent.Type.ThemeChange,QEvent.Type.WindowIconChange):
            self._last = None
            self.timer.start(0)
        elif event.type()==QEvent.Type.PlatformSurface:
            self._last = None
        return False

    def sync(self):
        if QApplication.platformName()!='windows' or getattr(self.window,'_closing',False):
            return
        hwnd = int(self.window.effectiveWinId())
        if hwnd: self.apply_to_handle(hwnd)

    def apply_to_handle(self,hwnd):
        if not hwnd or self.theme is None or getattr(self.window,'_closing',False):
            return
        key = (hwnd,*self.theme)
        if self._last==key: return
        try:
            if self.backend is None: self.backend = DwmCaptionApi()
            self.status = self.backend.apply(hwnd,*self.theme)
            if QApplication.platformName()=='windows':
                if self.identity_backend is None: self.identity_backend = CaptionIdentityApi()
                errors = self.identity_backend.hide(hwnd)
                if errors: self.status = replace(self.status,errors=self.status.errors+errors)
        except (OSError,AttributeError) as error:
            self.status = CaptionStatus(errors=(type(error).__name__,))
        self._last = key
        if self.status.errors and self.status.errors not in self._reported:
            self._reported.add(self.status.errors)
            self.log('原生标题栏部分外观设置失败，不支持的部分沿用系统外观：'+', '.join(self.status.errors)+'\n')
