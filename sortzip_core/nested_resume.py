"""Validate multi-layer checkpoints using only locally configured tools."""
import json
from pathlib import Path
import re

from sortzip_core.constants import RESUME_FILE_NAME, validate_win_folder_name
from sortzip_core.formats import validate_compression
from sortzip_core.nesting import Layer, MaterializedUnit, Plan, Task, Unit
from sortzip_core.nested_execution import _snapshot, execute_plan
from sortzip_core.archive_tools import TOOL_NAMES, entry_name, shown_name, validate_tool_layers
from sortzip_core.i18n import tr


def _relative(root, value, single=False):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(validate_win_folder_name(part) for part in value.split('/'))
            or (single and '/' in value)):
        raise ValueError(tr('恢复路径无效'))
    path = root/value
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(tr('恢复路径越界'))
    return path


def _metadata(value):
    if (not isinstance(value, list) or len(value)!=2 or
            any(type(n) is not int or n<0 for n in value)):
        raise ValueError(tr('文件元数据无效'))
    return tuple(value)


def load_resume(checkpoint, passwords=None, source_roots=None, *, inspect_only=False):
    """Return a validated plan/state without modifying disk.

    Initial inputs outside the output root require explicit trusted source_roots
    supplied by the caller, never roots read from the imported checkpoint.
    Passwords are keyed by zero-based layer index.
    """
    checkpoint = Path(checkpoint).resolve()
    root = checkpoint.parent
    if checkpoint.name != RESUME_FILE_NAME:
        raise ValueError(tr('请选择标准断点文件'))
    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or state.get('schema') not in (2,3,4) or state.get('kind')!='multilayer':
        raise ValueError(tr('不是有效的多层断点'))
    tool_id = 'bandizip'
    if state['schema'] in (3,4):
        tool_id = state.get('archive_tool')
        if not isinstance(tool_id,str) or tool_id not in TOOL_NAMES or type(state.get('output_list')) is not bool:
            raise ValueError(tr('断点的压缩工具或文件清单设置无效'))
    if not isinstance(state.get('tasks'), list) or not state['tasks']:
        raise ValueError(tr('断点缺少层计划'))
    sources, completed = state.get('sources'), state.get('completed')
    if not isinstance(sources, dict) or not sources or not isinstance(completed, dict):
        raise ValueError(tr('断点单元表无效'))
    roots = [root] + [Path(path).resolve() for path in (source_roots or ())]
    available = {}
    for identity, files in sources.items():
        if not isinstance(identity, str) or not identity or not isinstance(files, dict) or not files:
            raise ValueError(tr('源单元无效'))
        paths = []
        for name, info in files.items():
            if not isinstance(name, str) or not Path(name).is_absolute():
                raise ValueError(tr('源文件路径无效'))
            path = Path(name)
            if not any(path.resolve().is_relative_to(allowed) for allowed in roots):
                raise ValueError(tr('断点源文件不在调用者允许的目录中'))
            _metadata(info)
            paths.append(name)
        available[identity] = MaterializedUnit(tuple(paths), True)
    tasks, known, layer_settings = [], dict.fromkeys(sources, -1), {}
    unit_folders = {}
    used, previous = {}, set(sources)
    original_sets, source_seen = {}, set()
    last_layer = -1
    for row in state['tasks']:
        if not isinstance(row, dict):
            raise ValueError(tr('层计划无效'))
        identity, level, inputs = row.get('unit_id'), row.get('layer'), row.get('inputs')
        folder, stem = row.get('folder'), row.get('archive_stem')
        raw = row.get('settings')
        if (not isinstance(identity, str) or not identity or identity in known or
                type(level) is not int or level<0 or level not in (last_layer, last_layer+1)
                or not isinstance(inputs, list) or not inputs
                or any(not isinstance(child,str) for child in inputs) or len(inputs)!=len(set(inputs))):
            raise ValueError(tr('层顺序或单元关联无效'))
        _relative(root, folder)
        _relative(root, stem, True)
        if not isinstance(raw, dict) or set(raw)!=set(('group_size', 'archive_format', 'enable_volume',
                'volume', 'name_template', 'suffix', 'password_required', 'verify')):
            raise ValueError(tr('层参数无效'))
        if (any(type(raw[key]) is not bool for key in ('enable_volume', 'verify', 'password_required'))
                or not isinstance(raw['archive_format'], str) or not isinstance(raw['name_template'], str)):
            raise ValueError(tr('层参数类型无效'))
        if level in layer_settings and raw!=layer_settings[level]:
            raise ValueError(tr('同一层的参数不一致'))
        layer_settings[level] = raw
        if type(raw['group_size']) is not int or raw['group_size']<1 or len(inputs)>raw['group_size']:
            raise ValueError(tr('分组大小无效'))
        if level != last_layer:
            if last_layer>=0 and used[last_layer] != previous:
                raise ValueError(tr('上一层存在遗漏单元'))
            previous = {key for key, layer in known.items() if layer==level-1}
            used[level] = set()
        for child in inputs:
            if not isinstance(child, str) or known.get(child)!=level-1 or child in used[level]:
                raise ValueError(tr('单元被拆分、重复使用或跨层引用'))
            if child in unit_folders and unit_folders[child].casefold()!=folder.casefold():
                raise ValueError(tr('恢复计划跨越目录分组边界'))
            used[level].add(child)
        originals = row.get('original_ids')
        if not isinstance(originals, list) or not originals or any(not isinstance(v,str) or not v for v in originals):
            raise ValueError(tr('原始文件身份无效'))
        if level==0:
            if len(set(originals))!=len(originals) or source_seen.intersection(originals):
                raise ValueError(tr('原始文件身份重复'))
            source_seen.update(originals)
        elif originals != [v for child in inputs for v in original_sets[child]]:
            raise ValueError(tr('原始文件来源链不一致'))
        # Complete layers are consumed as archive files, so only pending
        # compression requires their original password.
        needs_password = raw['password_required'] and identity not in completed
        password = (passwords or {}).get(level, '') if raw['password_required'] else ''
        if not isinstance(password, str) or (needs_password and len(password)<8 and not inspect_only):
            raise ValueError(tr('第 {0} 层需要重新提供至少 8 位的原密码', level+1))
        validate_compression(dict(archive_format=raw['archive_format'], group_size=raw['group_size'],
            enable_volume=raw['enable_volume'], volume=raw['volume'], password=password,
            archive_suffix=raw['suffix'], double_compress=True),allow_rar=True)
        layer = Layer(raw['group_size'], raw['archive_format'], raw['enable_volume'], raw['volume'],
                      raw['name_template'], raw['suffix'], password, raw['verify'])
        tasks.append(Task(Unit(identity, folder, tuple(originals)), level, tuple(inputs), stem, layer))
        known[identity], original_sets[identity], last_layer = level, originals, level
        unit_folders[identity] = folder
    layers = {task.layer_index:task.settings for task in tasks}
    if state['schema']==4:
        from sortzip_core.resume_passwords import validate_checks
        validate_checks(state.get('password_checks'),{
            level for level,settings in layer_settings.items() if settings['password_required']})
    validate_tool_layers(tool_id,(layers[level] for level in range(last_layer+1)))
    state['archive_tool'] = tool_id
    if used[last_layer]!=previous:
        raise ValueError(tr('上一层存在遗漏单元'))
    final_ids = tuple(t.unit.unit_id for t in tasks if t.layer_index==last_layer)
    if state.get('final_ids')!=list(final_ids):
        raise ValueError(tr('最外层单元不一致'))
    if any(t.settings.suffix is not None and t.layer_index!=last_layer for t in tasks):
        raise ValueError(tr('中间层不能修改后缀'))
    if set(completed)!=set(t.unit.unit_id for t in tasks[:len(completed)]):
        raise ValueError(tr('已完成状态不满足依赖顺序'))
    for task in tasks[:len(completed)]:
        record = completed[task.unit.unit_id]
        if (not isinstance(record, dict) or record.get('layer')!=task.layer_index
                or record.get('inputs')!=list(task.input_ids) or record.get('original_ids')!=list(task.unit.original_ids)
                or record.get('format')!=task.settings.archive_format):
            raise ValueError(tr('完成记录与计划不一致'))
        folder = _relative(root, record.get('folder'))
        expected = root/f'layer-{task.layer_index+1}'/task.unit.folder
        if (folder.parent.resolve()!=expected.resolve() or not re.fullmatch(
                re.escape(task.archive_stem)+r'(?:\.retry-[0-9a-f]{32})?', folder.name)):
            raise ValueError(tr('产物目录与计划不一致'))
        mapping, metadata = record.get('files'), record.get('metadata')
        if not isinstance(mapping,list) or not mapping or not isinstance(metadata,dict):
            raise ValueError(tr('产物记录无效'))
        paths, canonical_names, folded_names = [], set(), set()
        for item in mapping:
            if not isinstance(item,dict):
                raise ValueError(tr('产物名称无效'))
            canonical = _relative(folder, item.get('canonical'), True).name
            shown = _relative(folder, item.get('shown'), True)
            if canonical.casefold() in folded_names:
                raise ValueError(tr('标准卷名重复'))
            folded_names.add(canonical.casefold())
            canonical_names.add(canonical)
            suffix = task.settings.suffix
            fmt = task.settings.archive_format
            expected_name = shown_name(canonical,task.archive_stem,fmt,suffix)
            if shown.name!=expected_name:
                raise ValueError(tr('展示卷名与后缀设置不一致'))
            if shown.name not in metadata:
                raise ValueError(tr('产物缺少元数据'))
            _metadata(metadata[shown.name])
            paths.append(str(shown))
        if len({p.casefold() for p in paths})!=len(paths) or set(metadata)!={Path(p).name for p in paths}:
            raise ValueError(tr('产物重复或元数据不一致'))
        entry_canonical = entry_name(tool_id,task.archive_stem,task.settings,canonical_names)
        expected_entry = next((item['shown'] for item in mapping if item['canonical']==entry_canonical), None)
        if record.get('entry')!=expected_entry or expected_entry is None:
            raise ValueError(tr('归档入口卷与格式不一致'))
        actual = {Path(p).name:values for p,values in _snapshot(paths).items()}
        if actual!={name:_metadata(info) for name,info in metadata.items()}:
            raise ValueError(tr('已完成产物缺失或发生变化'))
        if record.get('entry') not in metadata:
            raise ValueError(tr('归档入口卷缺失'))
        available[task.unit.unit_id] = MaterializedUnit(tuple(paths), True)
    # Only still-needed initial units need their source files to remain available.
    needed = {child for task in tasks[len(completed):] for child in task.input_ids if child in sources}
    if state['schema']!=4 or state.get('files_ready') is True:
        for identity in needed:
            if _snapshot(available[identity].files)!={name:_metadata(info) for name,info in sources[identity].items()}:
                raise ValueError(tr('待处理源文件发生变化'))
    return Plan(tuple(tasks), final_ids), available, state


def resume_plan(checkpoint, passwords=None, source_roots=None, **execution_options):
    from sortzip_core import engine, resume_passwords
    from sortzip_core.archive_tools import ArchiveTool, resolve_tool
    plan, available, state = load_resume(checkpoint, passwords, source_roots)
    if state['schema']==4:
        raise ValueError(tr('此断点必须通过主任务恢复入口打开'))
    root = Path(checkpoint).resolve().parent
    tool_id = state['archive_tool']
    tool = execution_options.get('archive_tool')
    if tool is not None and tool.identifier!=tool_id:
        raise ValueError(tr('恢复必须使用断点记录的压缩工具'))
    if tool is None:
        tool = ArchiveTool(tool_id,execution_options.get('bandizip','bz') if tool_id=='bandizip' else resolve_tool(tool_id))
    cancel = execution_options.get('cancel_check')
    force = execution_options.get('force_cancel_check')
    try:
        if not resume_passwords.verify(root,tool,plan,state,passwords or {},cancel,force):
            from sortzip_core.nested_execution import ExecutionResult
            return ExecutionResult(engine._cancel_status(force),available,dict(state['completed']))
    except engine.ForceCancelled:
        from sortzip_core.nested_execution import ExecutionResult
        return ExecutionResult('force_cancelled',available,dict(state['completed']))
    return execute_plan(plan, available, root,
                        _resume_state=state, **execution_options)
