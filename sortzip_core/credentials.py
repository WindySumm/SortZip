"""Windows generic credentials; no secrets are written to QSettings.

API reference: https://learn.microsoft.com/windows/win32/api/wincred/ns-wincred-credentialw
"""
import ctypes
from ctypes import wintypes

TARGET = 'SortZip/ArchivePassword'


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR)]


class WindowsCredentialStore:
    def __init__(self, target=TARGET):
        self.target = target

    def _api(self):
        api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_Credential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api

    def read(self):
        api, pointer = self._api(), ctypes.POINTER(_Credential)()
        if not api.CredReadW(self.target, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:
                return ''
            raise ctypes.WinError(error)
        try:
            cred = pointer.contents
            return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode('utf-16-le')
        finally:
            api.CredFree(pointer)

    def write(self, password):
        blob = password.encode('utf-16-le')
        if len(blob) > 2560:
            raise ValueError('密码超过 Windows 凭据存储大小限制')
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _Credential(Type=1, TargetName=self.target, CredentialBlobSize=len(blob),
                           CredentialBlob=buffer, Persist=2, UserName='SortZip')
        if not self._api().CredWriteW(ctypes.byref(cred), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self):
        if not self._api().CredDeleteW(self.target, 1, 0):
            error = ctypes.get_last_error()
            if error != 1168:
                raise ctypes.WinError(error)


def load_password(settings, store, warn):
    if not settings.value('remember_pwd', False, type=bool):
        save_password(settings, store, '', False, warn)
        return ''
    legacy = settings.value('password', '')
    try:
        if legacy:
            store.write(legacy)
            settings.remove('password')
            settings.sync()
            return legacy
        return store.read()
    except (OSError, ValueError, AttributeError):
        warn('无法读取或迁移已保存密码。请重新输入；旧密码仅在迁移成功后清除。')
        return ''


def save_password(settings, store, password, remember, warn):
    try:
        if remember and password:
            store.write(password)
        else:
            store.delete()
        settings.remove('password')
        settings.sync()
        return True
    except (OSError, ValueError, AttributeError):
        if not remember:
            settings.remove('password')
            settings.sync()
        warn('密码保存或清除失败，本次设置未完全生效；当前任务可继续。请检查 Windows 凭据管理器。')
        return False
