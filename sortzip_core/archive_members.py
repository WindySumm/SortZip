"""Verify the archive covers exactly the files eligible to replace its inputs."""
from collections import Counter
import gzip
from hashlib import sha256
import lzma
from pathlib import Path

from sortzip_core.constants import validate_win_folder_name
from sortzip_core.i18n import tr


def _names(tool, output):
    if tool=='winrar':
        return _rar_names(output)
    if tool!='7zip':
        return output.removesuffix('\n').split('\n') if output else []
    names = []
    for block in output.strip('\n').split('\n\n'):
        lines = block.split('\n')
        if (not lines or not lines[0].startswith('Path = ') or
                any(line.startswith('Path = ') for line in lines[1:]) or
                'Folder = +' in lines or
                any(line.startswith(('Attributes = D','Symbolic Link = ','Hard Link = ')) for line in lines)):
            raise ValueError(tr('无法读取归档成员列表'))
        names.append(lines[0][7:])
    return names


def _rar_names(output):
    names, continuing = [], None
    for block in output.strip('\n').split('\n\n'):
        fields = {}
        for line in block.split('\n'):
            key, sep, value = line.lstrip(' ').partition(': ')
            if sep and key in ('Target','Redirection'):
                raise ValueError(tr('归档成员包含无法识别的名称或意外路径'))
            if sep and key in ('Name','Type','Ratio'):
                if key in fields:
                    raise ValueError(tr('无法读取归档成员列表'))
                fields[key] = value
        if not fields:
            continue
        name, ratio = fields.get('Name'), fields.get('Ratio')
        if not name or fields.get('Type')!='File' or not ratio:
            raise ValueError(tr('无法读取归档成员列表'))
        if ratio in ('<->','<--'):
            if continuing!=name:
                raise ValueError(tr('无法读取归档成员列表'))
        else:
            if continuing is not None or (ratio!='-->' and not ratio.removesuffix('%').isdigit()):
                raise ValueError(tr('无法读取归档成员列表'))
            names.append(name)
        continuing = name if ratio in ('-->','<->') else None
    if continuing is not None:
        raise ValueError(tr('无法读取归档成员列表'))
    return names


def verify(tool, entry, settings, paths, force_cancel_check):
    from sortzip_core import engine

    if settings.archive_format in ('gz','xz'):
        try:
            _verify_stream(entry, settings.archive_format, paths[0], force_cancel_check)
        except (OSError, EOFError, lzma.LZMAError) as error:
            raise ValueError(tr('单流归档内容校验失败：{0}',entry.name)) from error
        return
    command,encoding = tool.list_command(entry,settings.password)
    try:
        output,_ = engine._run_proc(command,force_cancel_check,stdout_encoding=encoding,raw_stdout=True)
    except UnicodeError as error:
        raise ValueError(tr('无法读取归档成员列表')) from error
    names = _names(tool.identifier,output)
    invalid = [name for name in names if '/' in name or '\\' in name or '\ufffd' in name or validate_win_folder_name(name)]
    if invalid:
        raise ValueError(tr('归档成员包含无法识别的名称或意外路径：{0}',_summary(invalid)))
    actual = Counter(names)
    expected = Counter(Path(path).name for path in paths)
    if actual!=expected:
        missing, extra = expected-actual, actual-expected
        raise ValueError(tr('归档成员与计划不一致。缺少：{0}；多出或重复：{1}。',
                            _summary(missing),_summary(extra)))


def _summary(names):
    listed = ', '.join(repr(name) for name in list(names)[:8])
    return (listed + ' …') if len(names)>8 else listed or '—'


def _verify_stream(entry, archive_format, source, force_cancel_check):
    from sortzip_core import engine

    opener = gzip.open if archive_format=='gz' else lzma.open
    source_hash, archive_hash = sha256(), sha256()
    with Path(source).open('rb') as expected, opener(entry,'rb') as actual:
        while True:
            if force_cancel_check and force_cancel_check():
                raise engine.ForceCancelled()
            block = expected.read(1024*1024)
            decoded = actual.read(len(block) or 1)
            if len(decoded)!=len(block):
                break
            if not block:
                if archive_hash.digest()==source_hash.digest():
                    return
                break
            archive_hash.update(decoded)
            source_hash.update(block)
    raise ValueError(tr('单流归档内容校验失败：{0}',entry.name))
