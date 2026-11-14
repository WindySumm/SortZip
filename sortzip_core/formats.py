"""Format constraints shared by archivers; backend availability is separate.

https://www.bandisoft.com/bandizip/help/parameter/
GZ/XZ are single streams. Only ZIP/7Z split archives pass the full test/resume
workflow: TAR/GZ/XZ fragments fail bz t; LZH silently ignores -v.
"""
from sortzip_core.constants import validate_win_folder_name
import re
from sortzip_core.i18n import tr

FORMATS = {
    'zip': dict(password=True, volume=True, multiple=True),
    '7z': dict(password=True, volume=True, multiple=True),
    'tar': dict(password=False, volume=False, multiple=True),
    'gz': dict(password=False, volume=False, multiple=False),
    'xz': dict(password=False, volume=False, multiple=False),
    'lzh': dict(password=False, volume=False, multiple=True),
}
RAR_FORMAT = dict(password=True, volume=True, multiple=True)
LAYER_FORMATS = dict(FORMATS,rar=RAR_FORMAT)


def validate_volume(value):
    if value is not None and (not isinstance(value, str) or
            not re.fullmatch(r'[1-9]\d*(?:[kKmMgG](?:[bB])?)?', value)):
        raise ValueError(tr('分卷大小无效：请输入正整数，可带 k/m/g 或 KB/MB/GB；留空自动检测'))


def validate_compression(config, *, allow_rar=False):
    if not config.get('first_compress', True):
        return
    fmt = config.get('archive_format', 'zip')
    if fmt not in FORMATS and not (allow_rar and fmt=='rar'):
        raise ValueError(tr('不支持创建压缩格式：{0}', fmt))
    capability = RAR_FORMAT if fmt=='rar' else FORMATS[fmt]
    if config.get('password') and not capability['password']:
        raise ValueError(tr('{0} 不支持密码，请清空密码或选择 ZIP / 7Z', fmt.upper()))
    if config.get('enable_volume', True) and not capability['volume']:
        raise ValueError(tr('{0} 不支持本工具的分卷校验与恢复，请关闭分卷或选择 ZIP / 7Z', fmt.upper()))
    if config.get('enable_volume', True):
        validate_volume(config.get('volume'))
    if type(config.get('group_size', 1)) is not int or config.get('group_size', 1) < 1:
        raise ValueError(tr('每包文件数必须为正整数'))
    for key in ('archive_suffix', 'first_suffix_replace', 'first_suffix'):
        if key == 'archive_suffix' and not config.get('double_compress', True):
            continue
        if key == 'first_suffix_replace' and config.get('enable_volume', True):
            continue
        value = config.get(key)
        if value is None:
            continue
        if (not isinstance(value, str) or (not value and key != 'first_suffix')
                or validate_win_folder_name('archive' + value) or any(c in value for c in '*?[]')):
            raise ValueError(tr('压缩包后缀无效：{0}', key))
    if not capability['multiple']:
        if config.get('group_size', 1) != 1:
            raise ValueError(tr('{0} 每组只能包含一个文件，请将每包文件数设为 1', fmt.upper()))
        if config.get('double_compress', True) and config.get('enable_volume', True):
            raise ValueError(tr('{0} 二次打包不能容纳多个分卷，请关闭分卷或二次压缩', fmt.upper()))


def final_suffix(config):
    if config.get('double_compress', True):
        return config.get('archive_suffix') or '.' + config.get('archive_format', 'zip')
    if not config.get('enable_volume', True) and config.get('first_suffix_replace'):
        return config['first_suffix_replace']
    return '.' + config.get('archive_format', 'zip')
