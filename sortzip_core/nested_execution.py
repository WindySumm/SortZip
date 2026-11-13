"""Execute and checkpoint complete multi-layer archive units."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import uuid

from sortzip_core import engine
from sortzip_core.constants import validate_win_folder_name
from sortzip_core.nesting import MaterializedUnit, resolve_inputs
from sortzip_core.archive_tools import ArchiveTool, resolve_tool, shown_name, validate_tool_layers


@dataclass
class ExecutionResult:
    status: str
    units: dict
    records: dict
    error: str = ''


def task_specs(plan):
    specs = []
    for task in plan.tasks:
        settings = task.settings
        specs.append(dict(unit_id=task.unit.unit_id, folder=task.unit.folder,
            original_ids=list(task.unit.original_ids), inputs=list(task.input_ids),
            layer=task.layer_index, archive_stem=task.archive_stem,
            settings=dict(group_size=settings.group_size, archive_format=settings.archive_format,
                enable_volume=settings.enable_volume, volume=settings.volume,
                name_template=settings.name_template, suffix=settings.suffix,
                password_required=bool(settings.password), verify=settings.verify)))
    return specs


def _snapshot(paths):
    result, seen = {}, set()
    for path in paths:
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise ValueError('输入文件缺失或为符号链接')
        info = path.stat()
        key = str(path.resolve())
        if key.casefold() in seen:
            raise ValueError('输入文件路径重复')
        seen.add(key.casefold())
        result[key] = (info.st_size, info.st_mtime_ns)
    return result


def execute_plan(plan, inputs, destination, bandizip='bz', cancel_check=None,
                 force_cancel_check=None, on_progress=None, _resume_state=None,auto_close=True,
                 *, archive_tool=None, output_list=False):
    """Execute a fresh plan, publishing complete units only after persistence.

    Every input binding must already describe a complete unit. The filesystem is
    rechecked before/after each compression. No input is deleted, on any outcome.
    Persisted output names are relative; passwords are never serialized.
    The legacy auto_close argument is accepted for callers of older versions.
    """
    root = Path(destination).resolve()
    tool_id = _resume_state.get('archive_tool','bandizip') if _resume_state is not None else (
        archive_tool.identifier if archive_tool else 'bandizip')
    if archive_tool and archive_tool.identifier!=tool_id:
        raise ValueError('恢复必须使用断点记录的压缩工具')
    if _resume_state is not None and len(_resume_state['completed'])==len(plan.tasks):
        records = dict(_resume_state['completed'])
        return ExecutionResult('completed',dict(inputs),records)
    tool = archive_tool or ArchiveTool(tool_id,bandizip if tool_id=='bandizip' else resolve_tool(tool_id))
    layers = {task.layer_index:task.settings for task in plan.tasks}
    validate_tool_layers(tool_id,(layers[index] for index in sorted(layers)))
    if _resume_state is None and engine._checkpoint_path(root).exists():
        raise ValueError('输出已有恢复任务，不能覆盖')
    available = dict(inputs)
    records = dict(_resume_state['completed']) if _resume_state is not None else {}
    folders, planned_paths = {}, set()
    for task in plan.tasks:
        if validate_win_folder_name(task.archive_stem) or type(task.layer_index) is not int or task.layer_index < 0:
            raise ValueError('无效的层计划')
        folder = root/f'layer-{task.layer_index+1}'/task.unit.folder/task.archive_stem
        if not folder.resolve().is_relative_to(root) or folder.is_symlink():
            raise ValueError('压缩输出目录越界或已占用')
        key = str(folder.resolve()).casefold()
        if key in planned_paths:
            raise ValueError('压缩输出目录重复')
        planned_paths.add(key)
        if folder.exists():
            if _resume_state is None:
                raise ValueError('压缩输出目录已占用')
            folder = folder.with_name(folder.name+'.retry-'+uuid.uuid4().hex)
        folders[task.unit.unit_id] = folder
    root.mkdir(parents=True, exist_ok=True)
    if _resume_state is None:
        sources = {}
        for identity, unit in inputs.items():
            if unit.complete is not True:
                raise ValueError('初始输入单元未完整完成')
            sources[identity] = {path:list(values) for path, values in _snapshot(unit.files).items()}
        data = dict(schema=3, kind='multilayer', archive_tool=tool_id, output_list=output_list,
                    tasks=task_specs(plan), sources=sources, completed={}, final_ids=list(plan.final_ids))
        engine._write_checkpoint(root, data)
    else:
        data = _resume_state
    for index, task in enumerate(plan.tasks):
        if task.unit.unit_id in records:
            continue
        if engine._check_cancel(cancel_check):
            return ExecutionResult(engine._cancel_status(force_cancel_check), available, records)
        folder = folders[task.unit.unit_id]
        stage = '检查输入'
        try:
            paths = resolve_inputs(task, available)
            before = _snapshot(paths)
            # Flatten only after grouping full units; refuse ambiguous archive member names.
            names = [Path(path).name.casefold() for path in paths]
            if len(names) != len(set(names)):
                raise ValueError('输入归档成员同名，不能无损打包')
            folder.mkdir(parents=True)
            settings = task.settings
            canonical = folder/(task.archive_stem+'.'+settings.archive_format)
            volume = (settings.volume or engine.get_auto_volume(sum(v[0] for v in before.values()))) if settings.enable_volume else None
            stage = '创建归档'
            with tool.create_command(canonical,list(before),settings,volume) as command:
                print(f'{tool.name} 第 {task.layer_index+1} 层：{engine._command_text(command)}')
                engine._run_proc(command,force_cancel_check)
            stage = '检查归档及分卷'
            artifacts,entry = tool.collect(canonical,settings)
            if settings.verify or (tool_id!='bandizip' and entry!=canonical):
                stage = '完整性校验'
                engine._run_proc(tool.test_command(entry,settings.password),force_cancel_check)
            stage = '复核输入'
            if _snapshot(paths) != before:
                raise ValueError('压缩期间输入文件发生变化')
            stage = '发布产物名称'
            mapping = [dict(canonical=artifact.name,shown=shown_name(artifact.name,task.archive_stem,
                settings.archive_format,settings.suffix)) for artifact in artifacts]
            if len({item['shown'].casefold() for item in mapping}) != len(mapping):
                raise ValueError('后缀修改生成重复产物名称')
            source_names = {a.name.casefold() for a in artifacts}
            for item in mapping:
                if item['shown'] != item['canonical']:
                    target = folder/item['shown']
                    if validate_win_folder_name(item['shown']) or target.exists() or target.is_symlink() or item['shown'].casefold() in source_names:
                        raise ValueError('后缀修改目标无效或占用')
            for item in mapping:
                if item['shown'] != item['canonical']:
                    (folder/item['canonical']).rename(folder/item['shown'])
            files = tuple(str(folder/item['shown']) for item in mapping)
            entry_name = next(item['shown'] for item in mapping if item['canonical']==entry.name)
            record = dict(layer=task.layer_index, inputs=list(task.input_ids),
                original_ids=list(task.unit.original_ids), format=settings.archive_format,
                folder=folder.relative_to(root).as_posix(), entry=entry_name, files=mapping,
                metadata={Path(p).name:list(values) for p, values in _snapshot(files).items()})
            stage = '写入断点'
            data['completed'][task.unit.unit_id] = record
            engine._write_checkpoint(root, data)
            records[task.unit.unit_id] = record
            available[task.unit.unit_id] = MaterializedUnit(files, True)
            if on_progress:
                on_progress(index+1, len(plan.tasks), task.unit.unit_id)
        except engine.ForceCancelled:
            return ExecutionResult('force_cancelled', available, records)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            if isinstance(error,subprocess.CalledProcessError):
                reason = f'命令退出码 {error.returncode}'
            elif isinstance(error,FileNotFoundError):
                reason = '程序或文件缺失'
            else:
                reason = engine.redact(error,task.settings.password)
            message = f'{tool.name} 第 {task.layer_index+1} 层「{stage}」失败：{reason}。断点：{engine._checkpoint_path(root)}'
            print(message)
            return ExecutionResult('error',available,records,message)
    # Keep the schema-2 state until resume/portable restore integration is complete.
    return ExecutionResult('completed', available, records)
