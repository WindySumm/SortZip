"""Pure execution planning, naming rules and deterministic ordering (no Qt or I/O mutations)."""
import re
from dataclasses import dataclass
from collections import Counter
from pathlib import Path

from sortzip_core.constants import is_task_artifact, validate_win_folder_name
from sortzip_core.formats import final_suffix
from sortzip_core.i18n import tr


class StepError(ValueError):
    """Validation failure with an explicit workflow owner; text stays compatible."""
    def __init__(self,node,message):
        super().__init__(message)
        self.node = node


@dataclass(frozen=True)
class SourceRecord:
    path: Path
    size: int
    mtime_ns: int

def _natural_key(f):
    parts = re.split(r'(\d+)', f.name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]

SORT_FUNCS = {
    'name':        _natural_key,
    'name_desc':   _natural_key,
    'mtime':       lambda f: f.stat().st_mtime,
    'mtime_desc':  lambda f: (-f.stat().st_mtime, f.name),
    'size_asc':    lambda f: f.stat().st_size,
    'size_desc':   lambda f: (-f.stat().st_size, f.name),
    'ext':         lambda f: f.suffix,
}

def _sort_files(files, sort_by):
    fn = SORT_FUNCS.get(sort_by)
    if fn is None:
        fn = SORT_FUNCS['name']
    reverse = sort_by == 'name_desc'
    files.sort(key=fn, reverse=reverse)

def build_execution_plan(config, *, sources=None,inventory=None,context=None):
    """One ordered plan shared by preview, List.txt, rename and grouping.

    Folder keys and source identities are relative POSIX paths, never basenames.
    """
    if not config.get('src') or not config.get('dest'):
        raise ValueError(tr('请选择输入和输出文件夹'))
    root, dest = Path(config['src']).resolve(), Path(config['dest']).resolve()
    if root == dest:
        raise ValueError(tr('输入和输出文件夹不能相同'))
    if not root.is_dir():
        raise ValueError(tr('输入文件夹不存在'))
    size = config.get('group_size', 1)
    if type(size) is not int or size < 1:
        raise ValueError(tr('每包文件数必须为正整数'))
    records = {record.path:record for record in inventory} if inventory is not None else None
    iterator = records if records is not None else (sources if sources is not None else (root.rglob('*') if config.get('recursive') else root.iterdir()))
    grouped, used = {}, {}
    mappings = config.get('custom_names')
    for source in sorted(iterator, key=lambda p: p.relative_to(root).as_posix().casefold()):
        if context: context.check()
        if ((records is None and not source.is_file())
                or any(is_task_artifact(part) for part in source.relative_to(root).parts)
                or (dest.is_relative_to(root) and source.is_relative_to(dest))):
            continue
        category = (mappings.get(source.suffix.lower()) if mappings is not None
                    else source.suffix.lstrip('.') or 'no_extension')
        if category is None:
            continue
        if validate_win_folder_name(category):
            raise StepError('mapping',tr('分类名称无效：{0}', category))
        relative = source.relative_to(root)
        folder = Path(category) / (relative.parent if config.get('keep_hierarchy') else Path())
        folder_key = folder.as_posix()
        occupied = used.setdefault(folder_key.casefold(), set())
        classified, n = source.name, 0
        while classified.casefold() in occupied:
            n += 1
            classified = f'{source.stem}_{n}{source.suffix}'
        occupied.add(classified.casefold())
        info = records[source] if records is not None else source.stat()
        grouped.setdefault(folder_key, []).append(dict(source=source, source_rel=relative.as_posix(),
            folder=folder_key, classified_name=classified,
            size=info.size if records is not None else info.st_size,
            mtime_ns=info.mtime_ns if records is not None else info.st_mtime_ns))
    plan = []
    for folder, entries in sorted(grouped.items()):
        if context: context.check()
        sorted_sources = [e['source'] for e in entries]
        if records is None:
            _sort_files(sorted_sources, config.get('sort_by', 'name'))
        else:
            sort = config.get('sort_by','name')
            keys = dict(mtime=lambda p:records[p].mtime_ns,mtime_desc=lambda p:(-records[p].mtime_ns,p.name),
                size_asc=lambda p:records[p].size,size_desc=lambda p:(-records[p].size,p.name))
            sorted_sources.sort(key=keys.get(sort,SORT_FUNCS.get(sort,_natural_key)),reverse=sort=='name_desc')
        ranks = {source: i for i, source in enumerate(sorted_sources)}
        entries.sort(key=lambda e: ranks[e['source']])
        order = config.get('preview_order', {}).get(folder, [])
        ranks = {identity: i for i, identity in enumerate(order)}
        entries.sort(key=lambda e: ranks.get(e['source_rel'], ranks.get(e['source'].name, len(ranks))))
        rule = (_match_rule_hierarchy(config.get('naming_rules'), dest / folder, dest)
                if config.get('keep_hierarchy') else _match_rule(config.get('naming_rules'), folder))
        targets = set()
        for index, entry in enumerate(entries, 1):
            if context: context.check()
            classified = Path(entry['classified_name'])
            name = (render_template(rule.get('template', ''), index, classified.suffix,
                                    Path(folder).name, classified.stem) if rule else None) or classified.name
            if (validate_win_folder_name(name) or '/' in name or '\\' in name
                    or is_task_artifact(name) or (config.get('output_list') and name.casefold() == 'list.txt')):
                raise StepError('naming',tr('重命名目标无效或与对照表冲突：{0}/{1}', folder, name))
            if name.casefold() in targets:
                raise StepError('naming',tr('重命名目标重复：{0}/{1}', folder, name))
            targets.add(name.casefold())
            entry.update(target_name=name,index=index)
            plan.append(entry)
    refresh_group_metadata(plan,config,context=context)
    validate_plan_paths(plan,config,context=context)
    return plan


def validate_plan_paths(plan,config,*,context=None):
    """Recheck output topology even when file names come from a cached plan."""
    dest = Path(config['dest']).resolve()
    # Resolve directories once; each filename above has already been validated as
    # a single component. Executing an occupied (including symlink) target is refused.
    folders = {folder: (context.directory(dest/folder) if context else (dest/folder).resolve())
        for folder in {e['folder'] for e in plan}}
    for folder in folders.values():
        if not folder.is_relative_to(dest):
            raise ValueError(tr('输出子目录路径越界'))
    target_paths = [str(folders[e['folder']] / e['target_name']).casefold() for e in plan]
    if len(set(target_paths)) != len(target_paths):
        raise StepError('naming',tr('目标路径重复（Windows 不区分大小写）'))
    target_paths = set(target_paths)
    checked_groups = set()
    for entry in plan:
        if context: context.check()
        if config.get('first_compress', True) and config.get('compression_layers') is None:
            key = (entry['folder'], entry['group'])
            if key in checked_groups:
                continue
            checked_groups.add(key)
            folder = folders[entry['folder']]
            prefix = entry['group'] + ('-First' if config.get('double_compress', True) else '')
            fmt = config.get('archive_format', 'zip')
            outputs = [prefix + '.' + fmt, f"最终压缩{entry['group']}.{fmt}"]
            if config.get('first_suffix_replace') and not config.get('enable_volume', True):
                outputs.append(prefix + config['first_suffix_replace'])
            if config.get('double_compress', True):
                outputs.append(entry['group'] + final_suffix(config))
            if any(str(folder / name).casefold() in target_paths for name in outputs):
                raise ValueError(tr('压缩包与待压缩文件重名：{0}/{1}', entry["folder"], entry["group"]))

def refresh_group_metadata(entries,config,*,context=None):
    """Cheap group annotations also used when reusing a cached file-name plan."""
    counts = Counter(entry['folder'] for entry in entries)
    size = config.get('group_size',1)
    if type(size) is not int or size<1:
        raise ValueError(tr('每包文件数必须为正整数'))
    for entry in entries:
        if context: context.check()
        start = (entry['index']-1)//size*size+1
        end = min(start+size-1,counts[entry['folder']])
        base = str(start) if start==end else f'{start}-{end}'
        entry.update(group=base,archive=base+final_suffix(config) if config.get('first_compress',True) else '')
        if (config.get('first_compress',True) and not config.get('double_compress',True)
                and config.get('archive_format','zip')=='7z' and config.get('enable_volume',True)):
            entry['archive'] += tr('（分卷时为 {0}.7z.001…）', base)


def render_template(template, idx, ext, folder_name, original_name):
    if not template:
        return None
    return template.replace('{n}', str(idx)) \
                   .replace('{ext}', ext) \
                   .replace('{folder}', folder_name) \
                   .replace('{original}', original_name)

def _match_rule(naming_rules, folder_name):
    if not naming_rules:
        return None
    for rule in naming_rules:
        if not rule.get('enable', True):
            continue
        match = rule.get('match_folder', '')
        if match == '*' or match == folder_name:
            return rule
    return None

def _match_rule_hierarchy(naming_rules, folder, root=None):
    """Walk up ancestors to find a matching naming rule."""
    if not naming_rules:
        return None
    walk = folder
    root = Path(root) if root else None
    while True:
        rule = _match_rule(naming_rules, walk.name)
        if rule:
            return rule
        if root is not None and walk == root:
            break
        if walk.parent == walk:
            break
        walk = walk.parent
    return None

def check_naming_conflicts(folder_name, files, template):
    conflicts = []
    seen = {}
    for idx, file_path in enumerate(files, start=1):
        new_name = render_template(template, idx, file_path.suffix,
                                   folder_name, file_path.stem)
        if new_name in seen:
            conflicts.append((seen[new_name], file_path.name, new_name))
        else:
            seen[new_name] = file_path.name
    return conflicts
