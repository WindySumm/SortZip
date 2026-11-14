"""Portable, versioned metadata for a single chain of archive groups."""
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
import os

from sortzip_core.archive_tools import TOOL_FORMATS, entry_name, shown_name
from sortzip_core.constants import validate_win_folder_name
from sortzip_core import file_journal
from sortzip_core.i18n import tr
from sortzip_core.nesting import Layer


MEMBER = '.sortzip-delivery.json'
EXTENSION = '.sortzip.json'
FORMATS = {'zip','7z','rar'}
MAX_MANIFEST = 16*1024*1024


def name(value):
    if (not isinstance(value,str) or validate_win_folder_name(value)
            or any(char in value for char in '/\\') or value in ('.','..')
            or re.fullmatch(r'(?i)(CONIN\$|CONOUT\$|(?:COM|LPT)[¹²³])',value.split('.')[0].rstrip(' '))):
        raise ValueError(tr('解包清单包含无效文件名'))
    return value


def _require(condition):
    if not condition:
        raise ValueError(tr('解包清单结构无效'))


def _fields(obj, keys):
    _require(isinstance(obj,dict) and obj.keys()==set(keys.split()))


def _digest(value):
    _require(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value))


def _unique(names):
    folded = [name(item).casefold() for item in names]
    _require(len(folded)==len(set(folded)))


def validate_archive(obj):
    _fields(obj,'schema tool format stem entry encrypted volumes suffix')
    tool,fmt = obj['tool'],obj['format']
    _require(type(obj['schema']) is int and obj['schema']==1 and isinstance(tool,str)
             and isinstance(fmt,str) and fmt in FORMATS and fmt in TOOL_FORMATS.get(tool,{}))
    _require(type(obj['encrypted']) is bool and isinstance(obj['volumes'],list) and obj['volumes'])
    stem = name(obj['stem'])
    entry = name(obj['entry'])
    for row in obj['volumes']:
        _fields(row,'shown canonical size sha256')
        _require(type(row['size']) is int and row['size']>0)
        _digest(row['sha256'])
    names = [row['canonical'] for row in obj['volumes']]
    _unique(names)
    _unique([row['shown'] for row in obj['volumes']])
    canonical = entry_name(tool,stem,Layer(archive_format=fmt,enable_volume=True),set(names))
    entry_row = next(row for row in obj['volumes'] if row['canonical']==canonical)
    _require(entry==entry_row['shown'])
    suffix = obj['suffix']
    _require(suffix is None or (isinstance(suffix,str) and suffix.startswith('.')
                               and len(suffix)>1 and name(stem+suffix)))
    _require(all(shown_name(row['canonical'],stem,fmt,suffix)==row['shown'] for row in obj['volumes']))
    return obj


def validate_members(obj):
    _fields(obj,'schema layer children files')
    _require(type(obj['schema']) is int and obj['schema']==1 and type(obj['layer']) is int
             and 1<=obj['layer']<=128 and isinstance(obj['children'],list) and isinstance(obj['files'],list))
    paths,targets = [],[]
    if obj['layer']==1:
        _require(obj['files'] and not obj['children'])
        for row in obj['files']:
            _fields(row,'name size sha256 auxiliary' + (' original' if isinstance(row,dict) and 'original' in row else ''))
            _require(type(row['size']) is int and row['size']>=0 and type(row['auxiliary']) is bool)
            _digest(row['sha256'])
            paths.append(row['name'])
            if not row['auxiliary']:
                targets.append(row.get('original',row['name']))
            else:
                _require('original' not in row)
        _require(targets)
        _unique(targets)
    else:
        _require(obj['children'] and not obj['files'])
        for child in obj['children']:
            validate_archive(child)
            paths.extend(row['shown'] for row in child['volumes'])
    _unique([MEMBER,*paths])
    return obj


def read(path,validate):
    file_journal.identity(path)
    with path.open('rb') as stream:
        content = stream.read(MAX_MANIFEST+1)
    _require(len(content)<=MAX_MANIFEST)
    def unique(pairs):
        obj = dict(pairs)
        _require(len(obj)==len(pairs))
        return obj
    try:
        return validate(json.loads(content,object_pairs_hook=unique))
    except (UnicodeError,ValueError,RecursionError) as error:
        raise ValueError(tr('无法读取解包清单：{0}',path)) from error


def encode(obj):
    content = json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8')
    _require(len(content)<=MAX_MANIFEST)
    return content


def write_new(path,obj):
    content = encode(obj)
    if file_journal.exists(path):
        file_journal.identity(path)
        if path.stat().st_size!=len(content) or path.read_bytes()!=content:
            raise ValueError(tr('解包目标已存在：{0}',path))
        return
    with NamedTemporaryFile(dir=path.parent,prefix='.sortzip-delivery-',delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with temporary.open('wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        file_journal.rename_new(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def describe(path,shown=None,canonical=None,*,cancel=None):
    before = file_journal.identity(path)
    digest = file_journal.digest(path,cancel)
    if file_journal.identity(path)!=before:
        raise ValueError(tr('解包校验期间文件发生变化：{0}',path))
    return dict(shown=shown or path.name,canonical=canonical or path.name,size=before[2],sha256=digest)


def validate_plan(plan, entries, *, original, context=None):
    if len({task.layer_index for task in plan.tasks})>128 or any(task.settings.archive_format not in FORMATS for task in plan.tasks):
        raise ValueError(tr('自动解包仅支持最多 128 层的 ZIP、7Z 和 RAR5'))
    for row in entries:
        if context:
            context.check()
        name(row['target_name'])
        if row['target_name'].casefold()==MEMBER.casefold():
            raise ValueError(tr('解包清单包含无效文件名'))
    if any((task.settings.suffix or '').casefold()==EXTENSION for task in plan.tasks):
        raise ValueError(tr('解包清单名称与分卷冲突'))
    if original:
        from sortzip_core.restore_bundle import validate_payload
        validate_payload(plan,entries,context=context)


def inner_manifest(task, members, state, rows, *, cancel):
    if task.layer_index:
        children = [state['completed'][key]['delivery'] for key in task.input_ids]
        return validate_members(dict(schema=1,layer=task.layer_index+1,children=children,files=[]))
    source = dict(zip(members[:len(task.input_ids)],task.unit.original_ids,strict=True))
    files = []
    for member in members:
        path = Path(member)
        original = source.get(member)
        if original is None:
            info = describe(path,cancel=cancel)
            row = dict(name=path.name,size=info['size'],sha256=info['sha256'],auxiliary=True)
        else:
            info = rows[original]
            row = dict(name=path.name,size=info['identity'][2],sha256=info['sha256'],auxiliary=False)
            if state.get('delivery_original_names'):
                row['original'] = Path(original).name
        files.append(row)
    return validate_members(dict(schema=1,layer=1,children=[],files=files))


def archive_manifest(root,task,record,tool,*,cancel):
    folder = root/record['folder']
    if (task.archive_stem+EXTENSION).casefold() in {row['shown'].casefold() for row in record['files']}:
        raise ValueError(tr('解包清单名称与分卷冲突'))
    return validate_archive(dict(schema=1,tool=tool,format=record['format'],stem=task.archive_stem,
        entry=record['entry'],encrypted=bool(task.settings.password),suffix=task.settings.suffix,
        volumes=[describe(folder/row['shown'],row['shown'],row['canonical'],cancel=cancel)
                 for row in record['files']]))
