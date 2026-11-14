"""Bridge the existing file plan and multi-layer compression APIs."""
from pathlib import Path
import csv
import io
import json
import os
import uuid

from sortzip_core.archive_tools import ArchiveTool, TOOL_NAMES, check_tool, executable_identity, resolve_tool, validate_tool_layers
from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers
from sortzip_core.i18n import tr


def prepare_layers(config):
    raw = config.get('compression_layers')
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(tr('多层压缩至少需要一层配置'))
    try:
        layers = tuple(item if isinstance(item, Layer) else Layer(**item) for item in raw)
    except (TypeError, ValueError) as error:
        raise ValueError(tr('多层配置字段无效')) from error
    validate_tool_layers(config.get('archive_tool','bandizip'),layers)
    plan_layers((),layers,allow_rar=config.get('archive_tool')=='winrar')
    return layers


def preflight_tool(config):
    tool_id = config.get('archive_tool','bandizip')
    paths = config.get('archive_tool_paths')
    stamp = config.get('_archive_tool_identity')
    if stamp is not None:
        frozen = config['_archive_tool']
        try:
            unchanged = frozen.identifier==tool_id and executable_identity(frozen.executable)==stamp
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError(tr('{0} 程序已变化，请重新预检：{1}', TOOL_NAMES[tool_id], frozen.executable))
        return
    if config.get('_check_archive_executable'):
        selected = resolve_tool(tool_id,paths,config.get('bandizip'))
        before = executable_identity(selected)
        executable = check_tool(tool_id,{tool_id:selected})
        if executable!=selected or executable_identity(executable)!=before:
            raise ValueError(tr('压缩程序在检测期间发生变化：{0}', selected))
        config['_archive_tool_identity'] = before
    else:
        executable = check_tool(tool_id,paths,config.get('bandizip'))
    config['_archive_tool'] = ArchiveTool(tool_id,executable)


def attach_plan(config, *, context=None):
    """Attach dependencies and accurate human-readable outer archive locations."""
    entries = [dict(entry) for entry in config['execution_plan']]
    layers = prepare_layers(config)
    units = [Unit('source:'+entry['source_rel'], entry['folder'], (entry['source_rel'],)) for entry in entries]
    plan = plan_layers(units,layers,context=context,allow_rar=config.get('archive_tool')=='winrar')
    root = Path(config['dest']).resolve()
    file_paths = [(context.target(root/entry['folder']/entry['target_name']) if context else
        (root/entry['folder']/entry['target_name']).resolve()) for entry in entries]
    file_keys = {str(path).casefold() for path in file_paths}
    if any(not path.is_relative_to(root) for path in file_paths):
        raise ValueError(tr('输出文件路径越界'))
    file_ancestors = set(file_keys)
    for parent in {path.parent for path in file_paths}:
        if context: context.check()
        file_ancestors.update(str(path).casefold() for path in (parent,*parent.parents))
        if config.get('output_list'):
            listing = parent/'List.txt'
            if listing.exists() or listing.is_symlink():
                raise ValueError(tr('文件清单位置已占用：{0}', listing))
    if config.get('output_list') and any(str(parent/'List.txt').casefold() in file_ancestors for parent in {p.parent for p in file_paths}):
        raise ValueError(tr('文件或目录名称与文件清单 List.txt 冲突'))
    for task in plan.tasks:
        if context: context.check()
        folder = root/f'layer-{task.layer_index+1}'/task.unit.folder/task.archive_stem
        resolved = context.directory(folder) if context else folder.resolve()
        if not resolved.is_relative_to(root) or folder.exists() or folder.is_symlink():
            raise ValueError(tr('多层输出目录越界或已占用'))
        if str(resolved).casefold() in file_ancestors or any(str(parent).casefold() in file_keys for parent in resolved.parents):
            raise ValueError(tr('多层归档目录与分类后的文件路径冲突'))
    outer = {}
    final_ids = set(plan.final_ids)
    for task in plan.tasks:
        if task.unit.unit_id in final_ids:
            suffix = task.settings.suffix or '.'+task.settings.archive_format
            relative = f'layer-{task.layer_index+1}/{task.unit.folder}/{task.archive_stem}/{task.archive_stem}{suffix}'
            if task.settings.enable_volume:
                if task.settings.archive_format=='rar':
                    relative += tr('（分卷时在后缀前加入 .partN）')
                elif task.settings.archive_format=='7z' or config.get('archive_tool')=='7zip':
                    relative += tr('（入口卷以 .001 结尾）')
                else:
                    relative += tr('（ZIP 入口及卷名以实际产物为准）')
            for identity in task.unit.original_ids:
                outer[identity] = relative
    for entry in entries:
        if context: context.check()
        entry['archive'] = outer[entry['source_rel']]
    config['execution_plan'] = entries
    config['_nested_plan'] = plan
    return config


def execute(config, stats, on_progress=None, cancel_check=None, force_cancel_check=None):
    from sortzip_core.nested_execution import execute_plan
    plan = config['_nested_plan']
    root = Path(config['dest']).resolve()
    available = {'source:'+entry['source_rel']:MaterializedUnit(
        (str(root/entry['folder']/entry['target_name']),), True) for entry in config['execution_plan']}
    return _run(plan, available, root, config, stats, on_progress, cancel_check, force_cancel_check, execute_plan)


def resume(config, stats, on_progress=None, cancel_check=None, force_cancel_check=None):
    from sortzip_core import engine, resume_passwords
    from sortzip_core.nested_execution import execute_plan
    from sortzip_core.nested_resume import load_resume
    path = Path(config['resume_from']).resolve()
    plan, available, state = load_resume(path, config.get('layer_passwords'), config.get('source_roots'))
    config = dict(config,archive_tool=state['archive_tool'])
    if len(state['completed'])<len(plan.tasks):
        preflight_tool(config)
        try:
            if not resume_passwords.verify(path.parent,config['_archive_tool'],plan,state,
                    config.get('layer_passwords') or {},cancel_check,force_cancel_check):
                return engine._cancel_status(force_cancel_check)
        except engine.ForceCancelled:
            return 'force_cancelled'
    return _run(plan, available, path.parent, config, stats, on_progress, cancel_check,
                force_cancel_check, execute_plan, state)


def _run(plan, available, root, config, stats, on_progress, cancel_check, force_cancel_check, executor, state=None):
    total = len(plan.tasks)
    initial = len(state['completed']) if state is not None else 0
    stats.update(groups=initial, groups_total=total, groups_success=initial,
                 groups_failed=0, groups_skipped=0, groups_pending=total-initial)
    start = 0 if state is not None and config.get('resume_from') else 40
    def progress(done, count, identity):
        stats.update(groups=done, groups_success=done, groups_pending=count-done)
        if on_progress:
            on_progress(start, 100, done, count, tr('多层压缩：{0}/{1}', done, count))
    result = executor(plan, available, root, bandizip=config.get('bandizip', 'bz'),
        cancel_check=cancel_check, force_cancel_check=force_cancel_check,
        on_progress=progress, _resume_state=state,
        archive_tool=config.get('_archive_tool'),output_list=bool(config.get('output_list')))
    success = len(result.records)
    failed = int(result.status=='error' and success<total)
    stats.update(groups=success, groups_success=success, groups_failed=failed,
                 groups_pending=total-success-failed)
    if result.error:
        stats['error_detail'] = result.error
    if result.status=='completed':
        from sortzip_core import engine
        from sortzip_core.restore_bundle import write_bundle
        stage = tr('生成后缀还原脚本')
        try:
            bundles = {}
            for identity in plan.final_ids:
                record = result.records[identity]
                if not any(item['shown']!=item['canonical'] for item in record['files']):
                    continue
                folder = root/record['folder']
                kit = write_bundle(folder/'Restore-Archive-Names',[
                    (folder/item['shown'],item['canonical']) for item in record['files']])
                bundles[identity] = kit.relative_to(root).as_posix()
            state = json.loads(engine._checkpoint_path(root).read_text(encoding='utf-8'))
            state['restore_bundles'] = bundles
            if state.get('schema') in (3,4) and state['output_list']:
                stage = tr('生成文件清单')
                publish_lists(root,plan,result)
            stage = tr('写入断点')
            engine._write_checkpoint(root,state)
        except (OSError,ValueError) as error:
            tool_name = TOOL_NAMES[config.get('archive_tool','bandizip')]
            stats['error_detail'] = tr('{0}「{1}」失败：{2}。可从断点重新完成输出：{3}', tool_name, stage, error, engine._checkpoint_path(root))
            print(stats['error_detail'])
            return 'error'
    return result.status


def publish_lists(root,plan,result):
    """Publish exact outer entry paths after all units exist, including on retry."""
    outer = {}
    for identity in plan.final_ids:
        record = result.records[identity]
        canonical = next(row['canonical'] for row in record['files'] if row['shown']==record['entry'])
        relative = (Path(record['folder'])/record['entry']).as_posix()
        for original in record['original_ids']:
            outer[original] = relative, canonical, len(record['files'])
    contents = {}
    counts = {}
    for task in plan.tasks:
        if task.layer_index!=0:
            break
        folder = (root/task.unit.folder).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError(tr('文件清单目录无效'))
        if folder not in contents:
            contents[folder] = []
            counts[folder] = 0
        for source_id, original in zip(task.input_ids,task.unit.original_ids,strict=True):
            source = Path(result.units[source_id].files[0])
            if source.parent.resolve()!=folder:
                raise ValueError(tr('文件清单与分类位置不一致'))
            counts[folder] += 1
            contents[folder].append((counts[folder],original,source.name,*outer[original]))
    write_lists(contents,('序号','原文件名','新文件名','所属压缩包名','标准入口卷名','归档文件数'))


def write_lists(contents,header):
    for folder,rows in contents.items():
        stream = io.StringIO(newline='')
        writer = csv.writer(stream,delimiter='\t')
        writer.writerow(header)
        writer.writerows(rows)
        path = folder/'List.txt'
        content = stream.getvalue().encode('utf-8')
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes()!=content)):
            raise ValueError(tr('文件清单位置已占用：{0}', path))
        if path.exists():
            continue
        temporary = folder/f'.sortzip-list-{uuid.uuid4().hex}.tmp'
        try:
            with temporary.open('xb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if os.name=='nt':
                temporary.rename(path)
            else:
                os.link(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
