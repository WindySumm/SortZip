"""Read-only workspace scans and checkpoint inspection; independent of Qt."""
from copy import deepcopy
from dataclasses import dataclass,field
from pathlib import Path
import json
import sqlite3
from time import perf_counter

from sortzip_core.workspace_state import PreviewResult
from sortzip_core.planning import build_execution_plan,refresh_group_metadata,validate_plan_paths,StepError,SourceRecord
from sortzip_core.planning_context import PlanningContext,PlanningCancelled
from sortzip_core.nested_pipeline import attach_plan, preflight_tool
from sortzip_core.config import prepare_config
from sortzip_core.constants import is_task_artifact


def build_workspace_preview(config,revision,cancelled=lambda:False,strict=False):
    try:
        return _build_workspace_preview(config,revision,cancelled,strict)
    except PlanningCancelled:
        return None


def _build_workspace_preview(config,revision,cancelled,strict):
    result = PreviewResult(revision)
    context = PlanningContext(cancelled,cache_paths=not strict)
    started = perf_counter()
    cache = config.get('_preview_cache')
    config = deepcopy({key:value for key,value in config.items() if key!='_preview_cache'})
    try:
        root = Path(config['src']).resolve()
        if not config['src'] or not root.is_dir():
            raise ValueError('请选择有效的输入文件夹')
        if not config['dest'] and not strict:
            config['dest'] = str(root.parent/'SortZip_Preview_Output')
        destination = Path(config['dest']).resolve() if config['dest'] else None
        key = (str(root),bool(config.get('recursive')))
        cached = cache if not strict else None
        if cached is not None and cached.inventory_key==key and cached.inventory is not None:
            inventory = cached.inventory
        else:
            inventory = []
            iterator = root.rglob('*') if config.get('recursive') else root.iterdir()
            for source in iterator:
                if cancelled(): return None
                if source.is_file() and not is_task_artifact(source.name):
                    info = source.stat()
                    inventory.append(SourceRecord(source,info.st_size,info.st_mtime_ns))
            inventory = tuple(inventory)
        result.inventory,result.inventory_key = inventory,key
        result.timings['scan'] = perf_counter()-started
        started = perf_counter()
        exclude_destination = destination is not None and destination.is_relative_to(root)
        files = [record for record in inventory if not (exclude_destination and record.path.is_relative_to(destination))]
        extensions = {record.path.suffix.lower() for record in files}
        result.extensions = tuple(sorted(extensions))
        result.total_files = len(files)
        if cancelled():
            return None
        result.file_key = file_plan_key(config)
        if cached is not None and cached.file_key==result.file_key and not cached.file_error:
            result.file_plan = [dict(row) for row in cached.file_base]
            refresh_group_metadata(result.file_plan,config,context=context)
            validate_plan_paths(result.file_plan,config,context=context)
        else:
            result.file_plan = build_execution_plan(config,inventory=files,context=context)
        result.file_base = result.file_plan
        result.entries = [dict(source=row['source_rel'],folder=row['folder'],target=row['target_name'],
            classified=row['classified_name']) for row in result.file_plan]
        result.timings['file_plan'] = perf_counter()-started
    except (ValueError,OSError,TypeError) as error:
        result.file_error = str(error)
        if isinstance(error,StepError):
            result.node_errors[error.node] = str(error)
        return result
    if cancelled():
        return None
    try:
        started = perf_counter()
        config['execution_plan'] = result.file_plan
        if strict:
            config = prepare_config(config,require_files=True,context=context)
            if '_nested_plan' in config and config.get('_check_archive_executable'):
                preflight_tool(config)
            result.prepared = config
        elif config.get('compression_layers') is not None and config.get('first_compress'):
            attach_plan(config,context=context)
        result.file_plan = config['execution_plan']
        if config.get('_nested_plan') is not None:
            result.plan = config['_nested_plan']
        result.timings['archive_plan'] = perf_counter()-started
    except (ValueError,OSError,TypeError) as error:
        result.archive_error = str(error)
        if isinstance(error,StepError):
            result.node_errors[error.node] = str(error)
    return None if cancelled() else result


def file_plan_key(config):
    fields = ('src','dest','recursive','custom_names','naming_rules','sort_by','preview_order',
        'keep_hierarchy','output_list')
    key = {field:config.get(field) for field in fields}
    if config.get('compression_layers') is None:
        key['legacy_archive'] = {field:config.get(field) for field in ('group_size','first_compress',
            'archive_format','enable_volume','double_compress','first_suffix_replace','archive_suffix')}
    return json.dumps(key,sort_keys=True,ensure_ascii=False)


@dataclass
class ResumeInspection:
    path: str
    error: str = ''
    kind: str = ''
    archive_tool: str = 'bandizip'
    config: dict = field(default_factory=dict,repr=False)
    groups: list = field(default_factory=list,repr=False)
    problems: list = field(default_factory=list)
    plan: object = field(default=None,repr=False)
    available: dict = field(default_factory=dict,repr=False)
    completed: int = 0
    encrypted_layers: tuple = ()
    unverified_layers: tuple = ()
    input_root: str = ''
    tool_config: dict = field(default_factory=dict,repr=False)


def inspect_resume(config,revision,cancelled=lambda:False,strict=False):
    result = ResumeInspection(str(Path(config['resume_from']).resolve()))
    try:
        header = json.loads(Path(result.path).read_text(encoding='utf-8'))
        if not isinstance(header,dict):
            raise ValueError('断点结构无效')
        if header.get('schema')==4:
            from sortzip_core import file_journal, task_recovery
            state,operations = task_recovery.read_state(Path(result.path),state=header)
            root = Path(result.path).parent
            if not file_journal.exists(root/file_journal.DATABASE) and not state['files_ready']:
                result.input_root = state['input_root']
            else:
                try:
                    with file_journal.journal(root,state['job'],readonly=True) as db:
                        if task_recovery.input_needed(db,operations):
                            result.input_root = state['input_root']
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode!=sqlite3.SQLITE_READONLY_ROLLBACK:
                        raise
                    # A terminated SQLite transaction may need recovery by the locked writer.
                    if not state['files_ready']:
                        result.input_root = state['input_root']
            if state['kind']=='file_task':
                result.kind = 'file_task'
                result.groups = state['file_plan']
                result.completed = len(result.groups) if state['files_ready'] else 0
                return None if cancelled() else result
        if header.get('kind')=='multilayer':
            from sortzip_core.nested_resume import load_resume
            result.plan,result.available,state = load_resume(result.path,inspect_only=True)
            result.kind = 'multilayer'
            total = len(result.plan.tasks)
            result.archive_tool = state['archive_tool']
            result.completed = len(state['completed'])
            pending = {row['layer'] for row in state['tasks'] if row['settings']['password_required']
                       and row['unit_id'] not in state['completed']}
            result.encrypted_layers = tuple(sorted(pending))
            if state['schema']<4:
                observed = {row['layer'] for row in state['completed'].values()}
                result.unverified_layers = tuple(sorted(pending-observed))
        else:
            from sortzip_core.engine import _load_checkpoint,validate_resume_files
            data = _load_checkpoint(result.path)
            result.kind = 'legacy'
            result.config = data['config']
            result.groups = data['groups']
            if not result.groups:
                raise ValueError('断点没有分组信息')
            total = len(result.groups)
            result.completed = sum(row.get('state')=='final_done' for row in result.groups)
            result.problems = validate_resume_files(result.path)
            if result.completed<total and result.config.get('password'):
                from sortzip_core.resume_passwords import legacy_sample
                if legacy_sample(Path(result.path).parent,result.config,result.groups) is None:
                    result.unverified_layers = (0,1) if result.config.get('double_compress',True) else (0,)
        if config.get('_check_archive_executable') and result.completed<total and not cancelled():
            tool = dict(archive_tool=result.archive_tool,archive_tool_paths=config.get('archive_tool_paths'),
                        _check_archive_executable=True)
            try:
                preflight_tool(tool)
            except (ValueError,OSError) as error:
                raise ValueError(f'恢复前检测压缩工具失败：{error}。断点：{result.path}') from error
            result.tool_config = tool
    except (ValueError,OSError,TypeError,KeyError,sqlite3.Error) as error:
        result.error = str(error)
    return None if cancelled() else result
