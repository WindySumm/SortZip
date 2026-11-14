"""Select an application language at startup and translate display strings."""
from functools import lru_cache
import json
from pathlib import Path


LANGUAGE_KEY = 'ui_language'
LANGUAGE_CHOICES = (('system', 'System / 跟随系统'), ('zh', '简体中文'), ('en', 'English'))
_language = 'zh'


def language_preference(settings):
    value = settings.value(LANGUAGE_KEY, 'system', type=str)
    return value if value in ('system', 'zh', 'en') else 'system'


def resolve_language(preference, ui_languages):
    if preference in ('zh', 'en'):
        return preference
    for tag in ui_languages:
        code = tag.lower().replace('_', '-').partition('-')[0]
        if code in ('zh', 'en'):
            return code
    return 'en'


def configure_language(settings, app=None):
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    global _language
    _language = resolve_language(language_preference(settings), QLocale.system().uiLanguages())
    if app is not None and _language == 'zh':
        translator = QTranslator(app)
        if translator.load('qtbase_zh_CN', QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)


def current_language():
    return _language


@lru_cache(maxsize=1)
def _english_catalog():
    path = Path(__file__).with_name('translations') / 'en.json'
    return json.loads(path.read_text(encoding='utf-8'))


def tr(message, *values):
    if _language == 'en':
        message = _english_catalog().get(message, message)
    return message.format(*values) if values else message
