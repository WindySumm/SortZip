"""Versioned per-archive and task-wide delivery manifests."""
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from itertools import count
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
MAX_LAYERS = 128


def uses_task_manifest(config):
    return (bool(config.get('managed_recovery'))
            and config.get('archive_tool','bandizip') in ('bandizip','7zip'))


def name(value):
    if (not isinstance(value,str) or validate_win_folder_name(value)
            or any(char in value for char in '/\\') or value in ('.','..')
            or re.fullmatch(r'(?i)(CONIN\$|CONOUT\$|(?:COM|LPT)[¹²³])',value.split('.')[0].rstrip(' '))):
        raise ValueError(tr('解包清单包含无效文件名'))
    return value


def choose_task_manifest_name(root,folders,started_at):
    categories = {}
    for folder in folders:
        category = Path(folder).parts[0]
        categories.setdefault(category.casefold(),category)
    label = next(iter(categories.values()))[:40].rstrip(' .') or 'SortZip'
    if len(categories)>1:
        label += f'等{len(categories)}类'
    stem = f"{label}-{started_at:%Y-%m-%d_%H%M}"
    occupied = set(categories)
    occupied.update(item.name.casefold() for item in root.iterdir())
    for number in count(1):
        suffix = '' if number==1 else f'-{number}'
        candidate = name(stem+suffix+EXTENSION)
        if candidate.casefold() not in occupied:
            return candidate


def task_manifest_name(state):
    if 'delivery_manifest_name' in state:
        value = name(state['delivery_manifest_name'])
        _require(state.get('task_delivery') is True and len(value)>len(EXTENSION)
                 and value.endswith(EXTENSION))
        return value
    return 'SortZip-'+state['job']+EXTENSION


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
             and 1<=obj['layer']<=MAX_LAYERS and isinstance(obj['children'],list) and isinstance(obj['files'],list))
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


def validate_outer(obj):
    if isinstance(obj,dict) and type(obj.get('schema')) is int and obj['schema']==2:
        count = obj.get('layer_count')
        _require(type(count) is int and 1<=count<=MAX_LAYERS)
        archive = dict(obj, schema=1)
        del archive['layer_count']
        validate_archive(archive)
        return obj
    return validate_archive(obj)


def write_outer(path,archive,layer_count):
    manifest = validate_outer(dict(archive,schema=2,layer_count=layer_count))
    if file_journal.exists(path) and read(path,validate_outer)==archive:
        # A previous release may have published this manifest before its final checkpoint write.
        return
    write_new(path,manifest)


def relative_directory(value):
    _require(isinstance(value,str))
    parts = value.split('/') if value else []
    for part in parts:
        name(part)
        _require(not part.casefold().startswith('.sortzip-'))
    return value


def _directory_conflict(path, paths):
    parents = path.split('/')[:-1]
    return any('/'.join(parents[:index]) in paths for index in range(1,len(parents)+1))


def validate_delivery(obj):
    if not isinstance(obj,dict) or obj.get('kind')!='sortzip-task':
        return validate_outer(obj)
    _fields(obj,'schema kind groups'+(' passwords' if 'passwords' in obj else ''))
    if 'passwords' in obj:
        from sortzip_core.delivery_passwords import valid_envelope
        _require(valid_envelope(obj['passwords']))
    _require(type(obj['schema']) is int and obj['schema']==1 and isinstance(obj['groups'],list) and obj['groups'])
    volumes = set()

    def inspect(node,level,tool,targets):
        _fields(node,'archive members children')
        archive = validate_archive(node['archive'])
        _require(archive['tool']==tool)
        data = node['members']
        _require(isinstance(data,dict) and data.get('layer')==level)
        if level==1:
            _require(isinstance(data.get('files'),list))
            rows = []
            for row in data['files']:
                _require(isinstance(row,dict) and type(row.get('auxiliary')) is bool)
                row = dict(row)
                if not row['auxiliary']:
                    _require('directory' in row)
                    directory = relative_directory(row.pop('directory'))
                    filename = name(row.get('name'))
                    for enabled in (False,True):
                        target_name = name(row.get('original',filename)) if enabled else filename
                        _require(not target_name.casefold().startswith('.sortzip-'))
                        target = '/'.join(filter(None,(directory,target_name))).casefold()
                        _require(target not in targets[enabled])
                        targets[enabled].add(target)
                rows.append(row)
            validate_members(dict(data,files=rows))
        else:
            validate_members(data)
        children = node['children']
        _require(isinstance(children,list) and len(children)==len(data['children']))
        for child,descriptor in zip(children,data['children'],strict=True):
            inspect(child,level-1,tool,targets)
            _require(child['archive']==descriptor)

    targets = {False:set(),True:set()}
    for group in obj['groups']:
        _fields(group,'folder layer_count tree')
        folder = relative_directory(group['folder'])
        count = group['layer_count']
        _require(type(count) is int and 1<=count<=MAX_LAYERS)
        tree = group['tree']
        _require(isinstance(tree,dict) and isinstance(tree.get('archive'),dict))
        inspect(tree,count,tree['archive'].get('tool'),targets)
        for row in tree['archive']['volumes']:
            for filename in {row['shown'].casefold(),row['canonical'].casefold()}:
                key = (folder.casefold(),filename)
                _require(key not in volumes)
                volumes.add(key)
    for paths in targets.values():
        _require(not any(_directory_conflict(path,paths) for path in paths))
    return obj


def validate_source_paths(entries,original):
    targets = {False:set(),True:set()}
    for entry in entries:
        source = entry['source_rel'].split('/')
        folder = relative_directory('/'.join(source[:-1]))
        for restore in targets:
            filename = source[-1] if restore and original else entry['target_name']
            name(filename)
            _require(not filename.casefold().startswith('.sortzip-'))
            identity = '/'.join(filter(None,(folder,filename)))
            folded = identity.casefold()
            if folded in targets[restore]:
                raise ValueError(tr('解包时文件名会冲突：{0}',identity))
            targets[restore].add(folded)
    for files in targets.values():
        for path in files:
            if _directory_conflict(path,files):
                raise ValueError(tr('解包时文件名会冲突：{0}',path))


def write_task(root,plan,state,config):
    records = state['completed']

    def tree(identity):
        record = records[identity]
        members = record['delivery_members']
        return dict(archive=record['delivery'],members=members,
                    children=[tree(child) for child in record['inputs']] if record['layer'] else [])

    groups = [dict(folder=records[key]['folder'],layer_count=records[key]['layer']+1,tree=tree(key))
              for key in plan.final_ids]
    manifest = validate_delivery(dict(schema=1,kind='sortzip-task',groups=groups))
    path = file_journal.safe_path(root,task_manifest_name(state))
    mode = state.get('delivery_password_mode','none')
    if mode!='none':
        from sortzip_core.delivery_passwords import protect,unlock
        from sortzip_core.resume_passwords import verify_delivery_passwords
        passwords = {str(task.layer_index):task.settings.password for task in plan.tasks if task.settings.password}
        verify_delivery_passwords(state,passwords)
        phrase = config.get('delivery_phrase','')
        if file_journal.exists(path):
            existing = read(path,validate_delivery)
            envelope = existing.get('passwords',{})
            if (dict((key,value) for key,value in existing.items() if key!='passwords')==manifest
                    and envelope.get('mode')==mode and unlock(existing,phrase)==passwords):
                return
            raise ValueError(tr('输出位置已被占用：{0}',path))
        manifest['passwords'] = protect(manifest,mode,passwords,phrase)
        validate_delivery(manifest)
    write_new(path,manifest)


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
    if len({task.layer_index for task in plan.tasks})>MAX_LAYERS or any(task.settings.archive_format not in FORMATS for task in plan.tasks):
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
            if state.get('task_delivery'):
                row['directory'] = '/'.join(original.split('/')[:-1])
        files.append(row)
    payload = dict(schema=1,layer=1,children=[],files=files)
    validate_members(dict(payload,files=[{key:value for key,value in row.items() if key!='directory'}
                                        for row in files]))
    return payload


def archive_manifest(root,task,record,tool,*,cancel):
    folder = root/record['folder']
    if (task.archive_stem+EXTENSION).casefold() in {row['shown'].casefold() for row in record['files']}:
        raise ValueError(tr('解包清单名称与分卷冲突'))
    return validate_archive(dict(schema=1,tool=tool,format=record['format'],stem=task.archive_stem,
        entry=record['entry'],encrypted=bool(task.settings.password),suffix=task.settings.suffix,
        volumes=[describe(folder/row['shown'],row['shown'],row['canonical'],cancel=cancel)
                 for row in record['files']]))
