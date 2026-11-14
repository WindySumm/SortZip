import ast
import os
from pathlib import Path
import string
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sortzip_core import i18n


class LanguageTests(unittest.TestCase):
    def test_saved_preference_and_ordered_system_fallback(self):
        self.assertEqual(i18n.resolve_language('en',['zh-Hans-CN']), 'en')
        self.assertEqual(i18n.resolve_language('zh',['en-GB']), 'zh')
        self.assertEqual(i18n.resolve_language('system',['fr-FR','zh-Hant-TW','en-US']), 'zh')
        self.assertEqual(i18n.resolve_language('system',['en-US','zh-Hans']), 'en')
        self.assertEqual(i18n.resolve_language('system',['ja-JP']), 'en')
        settings = SimpleNamespace(value=lambda *args, **kwargs:'invalid')
        self.assertEqual(i18n.language_preference(settings), 'system')

    def test_english_preserves_template_variables_and_inserted_paths(self):
        with patch.object(i18n,'_language','en'):
            self.assertEqual(i18n.tr('输出目录：{0}','D:/示例/{output}'), 'Output folder: D:/示例/{output}')
            self.assertIn('{source_range}',i18n.tr('压缩包：{n} 本层组序号 · {original} 本层输入单元范围 · {source_range} 原文件序号范围 · {folder} 目录名。扩展名由压缩格式附加。'))

    def test_display_messages_have_english_translations(self):
        root = Path(i18n.__file__).parent
        formatter = string.Formatter()
        catalog = i18n._english_catalog()
        for file in root.glob('*.py'):
            for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
                if (isinstance(node,ast.Call) and isinstance(node.func,ast.Name)
                        and node.func.id in ('tr','structural') and node.args
                        and isinstance(node.args[0],ast.Constant)):
                    source = node.args[0].value
                    self.assertTrue(source in catalog,(file.name,node.lineno,source))
                    fields = lambda pattern: sorted(field for _,field,_,_ in formatter.parse(pattern) if field is not None)
                    self.assertEqual(fields(source),fields(catalog[source]),source)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','Run with Windows integration tests')
    def test_settings_saves_english_and_next_window_uses_it(self):
        from tempfile import TemporaryDirectory
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication,QComboBox,QDialog
        from sortzip_core.app import MainWindow
        from sortzip_core.smoke import _MemoryCredentials

        app = QApplication.instance() or QApplication([])
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=scratch) as folder, patch.object(i18n,'_language','zh'):
            settings = QSettings(str(Path(folder)/'settings.ini'),QSettings.Format.IniFormat)
            settings.setValue(i18n.LANGUAGE_KEY,'zh')
            window = MainWindow(settings=settings,credential_store=_MemoryCredentials(),check_updates=False)

            def select_english(dialog):
                selector = dialog.findChild(QComboBox,'InterfaceLanguage')
                self.assertIsNotNone(selector)
                selector.setCurrentIndex(selector.findData('en'))

            with patch.object(QDialog,'exec',lambda dialog:(select_english(dialog),QDialog.DialogCode.Rejected)[1]):
                window.show_settings()
            self.assertEqual(i18n.language_preference(settings),'zh')
            with patch.object(QDialog,'exec',lambda dialog:(select_english(dialog),QDialog.DialogCode.Accepted)[1]):
                window.show_settings()
            self.assertEqual(i18n.language_preference(settings),'en')
            self.assertEqual(window.run_btn.text(),'开始执行')
            window.close()
            app.processEvents()

            i18n.configure_language(settings)
            window = MainWindow(settings=settings,credential_store=_MemoryCredentials(),check_updates=False)
            try:
                self.assertEqual(window.run_btn.text(),'Start task')
                self.assertEqual(window.preview_tabs.tabText(1),'Live filename preview')
                self.assertEqual(window.preset_category.itemText(0),'Videos')
                self.assertEqual(window.preset_category.itemData(0),'视频')
            finally:
                window.close()
                app.processEvents()
