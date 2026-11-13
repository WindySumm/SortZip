"""Native-frame coloring changes no window controls or application identity."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock

from sortzip_core.native_caption import (DwmCaptionApi,CaptionStatus,NativeCaption,CaptionIdentityApi,
    colorref,DARK_MODE,CAPTION_COLOR,TEXT_COLOR,BORDER_COLOR,COLOR_DEFAULT)


class Setter:
    def __init__(self,failed=()):
        self.calls = []
        self.failed = failed

    def __call__(self,hwnd,attribute,pointer,size):
        value = ctypes.cast(pointer,ctypes.POINTER(ctypes.c_uint32)).contents.value
        self.calls.append((hwnd.value,attribute,value,size))
        return -2147024809 if attribute in self.failed else 0


class DwmCaptionTests(unittest.TestCase):
    def api(self,build=26100,failed=()):
        setter = Setter(failed)
        return DwmCaptionApi(library=SimpleNamespace(DwmSetWindowAttribute=setter),build=build),setter

    def test_colorref_channel_order_and_invalid_color(self):
        self.assertEqual(colorref('#123456'),0x563412)
        with self.assertRaises(ValueError): colorref('not a color')

    def test_windows_11_applies_matching_background_and_readable_foreground(self):
        api,setter = self.api()
        status = api.apply(123,True,'#171d25','#edf2f7')
        self.assertTrue(status.dark_mode and status.custom_colors)
        self.assertFalse(status.errors)
        values = {attribute:value for hwnd,attribute,value,size in setter.calls}
        self.assertEqual(values[DARK_MODE],1)
        self.assertEqual(values[CAPTION_COLOR],colorref('#171d25'))
        self.assertEqual(values[TEXT_COLOR],colorref('#edf2f7'))
        self.assertEqual(values[BORDER_COLOR],values[CAPTION_COLOR])
        self.assertTrue(all(size==4 for hwnd,attribute,value,size in setter.calls))
        self.assertEqual(len(setter.argtypes),4)

    def test_windows_10_only_requests_dark_mode_and_light_restores_it(self):
        api,setter = self.api(19045)
        self.assertTrue(api.apply(123,True,'#171d25','#edf2f7').dark_mode)
        self.assertFalse(api.apply(123,False,'#f7f7f8','#202124').dark_mode)
        self.assertEqual([(attribute,value) for hwnd,attribute,value,size in setter.calls],[(20,1),(20,0)])

    def test_partial_color_failure_resets_both_colors_instead_of_leaving_bad_contrast(self):
        api,setter = self.api(failed=(TEXT_COLOR,))
        status = api.apply(123,True,'#171d25','#edf2f7')
        self.assertFalse(status.custom_colors)
        self.assertTrue(status.errors)
        final = {attribute:value for hwnd,attribute,value,size in setter.calls}
        self.assertEqual(final[CAPTION_COLOR],COLOR_DEFAULT)
        self.assertEqual(final[TEXT_COLOR],COLOR_DEFAULT)
        self.assertEqual(final[BORDER_COLOR],COLOR_DEFAULT)

    def test_unsupported_attributes_report_failure_without_raising(self):
        api,setter = self.api(19045,failed=(DARK_MODE,))
        status = api.apply(123,True,'#171d25','#edf2f7')
        self.assertFalse(status.dark_mode or status.custom_colors)
        self.assertEqual(status.errors,('20:0x80070057',))


class CaptionIdentityTests(unittest.TestCase):
    def api(self,style=0x40000):
        theme = SimpleNamespace(SetWindowThemeAttribute=Mock(return_value=0))
        user = SimpleNamespace(GetWindowLongW=Mock(return_value=style),
            SetWindowLongW=Mock(return_value=style),SetWindowPos=Mock(return_value=1),
            SendMessageW=Mock(return_value=123))
        return CaptionIdentityApi(theme,user),theme,user

    def test_caption_suppression_keeps_taskbar_style_and_big_icon(self):
        api,theme,user = self.api()
        self.assertFalse(api.hide(123))
        self.assertEqual(user.SetWindowLongW.call_args.args[1:],(-20,0x40001))
        self.assertEqual(user.SendMessageW.call_args.args[1:],(0x80,0,0))
        options = ctypes.cast(theme.SetWindowThemeAttribute.call_args.args[2],ctypes.POINTER(api.Options)).contents
        self.assertEqual((options.flags,options.mask),(3,3))
        self.assertEqual(user.SetWindowPos.call_args.args[2:],(0,0,0,0,0x37))

    def test_unsupported_theme_does_not_modify_native_styles_or_icons(self):
        api,theme,user = self.api()
        theme.SetWindowThemeAttribute.return_value = -1
        self.assertTrue(api.hide(123))
        user.SetWindowLongW.assert_not_called()
        user.SendMessageW.assert_not_called()

    def test_existing_dialog_frame_does_not_repeat_frame_geometry_update(self):
        api,theme,user = self.api(0x40001)
        self.assertFalse(api.hide(123))
        user.SetWindowLongW.assert_not_called()
        user.SetWindowPos.assert_not_called()
        user.SendMessageW.assert_called_once()


class CaptionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QWidget
        self.window = QWidget()
        self.messages = []
        self.caption = NativeCaption(self.window,self.messages.append)
        self.calls = []
        self.caption.backend = SimpleNamespace(apply=lambda *args:self.calls.append(args) or CaptionStatus(True,True))
        self.caption.set_theme(True,'#171d25','#edf2f7')
        self.addCleanup(self.window.close)

    def test_new_handle_and_theme_reapply_but_duplicate_request_does_not(self):
        from PySide6.QtCore import QEvent
        self.caption.apply_to_handle(101)
        self.caption.apply_to_handle(101)
        self.caption.eventFilter(self.window,QEvent(QEvent.Type.WinIdChange))
        self.caption.apply_to_handle(202)
        self.caption.set_theme(False,'#f7f7f8','#202124')
        self.caption.apply_to_handle(202)
        self.assertEqual([call[0] for call in self.calls],[101,202,202])
        self.assertFalse(self.calls[-1][1])

    def test_closing_and_offscreen_do_not_call_native_api(self):
        self.caption.sync()
        self.assertFalse(self.calls)
        self.window._closing = True
        self.caption.apply_to_handle(101)
        self.assertFalse(self.calls)

    def test_missing_library_is_nonfatal_and_logged_once(self):
        self.caption.backend = None
        with patch('sortzip_core.native_caption.DwmCaptionApi',side_effect=OSError('unavailable')):
            self.caption.apply_to_handle(101)
            self.caption.apply_to_handle(202)
        self.assertEqual(len(self.messages),1)
        self.assertFalse(self.caption.status.custom_colors)


if __name__=='__main__': unittest.main()
