"""Bridge the existing file plan and multi-layer compression APIs."""
from pathlib import Path
import csv
import io
import json
import os
import uuid
from itertools import zip_longest

from sortzip_core.archive_tools import ArchiveTool, TOOL_NAMES, check_tool, executable_identity, resolve_tool, shown_name, validate_tool_layers
from sortzip_core import archive_publication, file_journal
from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers
from sortzip_core.i18n import current_language, tr


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
    if config.get('restore_original_names'):
        from sortzip_core.restore_bundle import validate_payload
        validate_payload(plan,entries,context=context)
    task_manifest = False
    if config.get('delivery_manifest'):
        from sortzip_core.delivery_manifest import uses_task_manifest,validate_plan,validate_source_paths
        task_manifest = uses_task_manifest(config)
        validate_plan(plan,entries,original=config.get('delivery_original_names',False),context=context)
        if task_manifest:
            validate_source_paths(entries,config.get('delivery_original_names',False))
    root = Path(config['dest']).resolve()
    source = Path(config['src']).resolve()
    file_paths = [(context.target(root/entry['folder']/entry['target_name']) if context else
        (root/entry['folder']/entry['target_name']).resolve()) for entry in entries]
    if any(not path.is_relative_to(root) for path in file_paths):
        raise ValueError(tr('输出文件路径越界'))
    classified = {root/entry['folder']/entry['classified_name'] for entry in entries}
    directories = {path.parent for path in file_paths}
    targets = {parent/'List.txt' for parent in directories} if config.get('output_list') else set()
    script_layout = dict(category_restore_layout=archive_publication.CATEGORY_FILE_LAYOUT,
                         script_language=current_language())
    volumes = []
    final_ids = set(plan.final_ids)
    if plan.tasks:
        retained = config.get('retain_layers')
        if retained is None:
            retained = range(plan.tasks[-1].layer_index)
        for task in archive_publication.kept(plan,{'retain_layers':retained}):
            if context: context.check()
            folder = root/archive_publication.folder_for(task,final_ids)
            directories.add(folder)
            canonical = task.archive_stem+'.'+task.settings.archive_format
            shown = shown_name(canonical,task.archive_stem,task.settings.archive_format,task.settings.suffix)
            volumes.append(folder/shown)
            if canonical.casefold()!=shown.casefold():
                volumes.append(folder/canonical)
            if task.unit.unit_id in final_ids:
                if config.get('delivery_manifest') and not task_manifest:
                    targets.add(folder/(task.archive_stem+'.sortzip.json'))
                if shown!=canonical:
                    targets.update(archive_publication.restore_paths(root/Path(task.unit.folder).parts[0],script_layout))
    archive_publication.check_destinations(root,source,[*volumes,*sorted(targets)],directories,
                                           [*file_paths,*classified],context=context)
    outer = {}
    final_ids = set(plan.final_ids)
    for task in plan.tasks:
        if task.unit.unit_id in final_ids:
            suffix = task.settings.suffix or '.'+task.settings.archive_format
            relative = f'{task.unit.folder}/{task.archive_stem}{suffix}'
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


def _run(plan, available, root, config, stats, on_progress, cancel_check, force_cancel_check, executor, state=None,
         *, cleanup=None):
    total = len(plan.tasks)
    initial = len(state['completed']) if state is not None else 0
    stats.update(groups=initial, groups_total=total, groups_success=initial,
                 groups_failed=0, groups_skipped=0, groups_pending=total-initial)
    start = 0 if state is not None and config.get('resume_from') else 40
    def progress(done, count, identity):
        stats.update(groups=done, groups_success=done, groups_pending=count-done)
        if on_progress:
            on_progress(start, 100, done, count, tr('多层压缩：{0}/{1}', done, count))
    options = {} if cleanup is None else {'cleanup':cleanup}
    if on_progress:
        options['on_space_warning'] = lambda done,count,warning:on_progress(
            start,100,done,count,tr('磁盘空间估算警告：{0}',warning))
    result = executor(plan, available, root, bandizip=config.get('bandizip', 'bz'),
        cancel_check=cancel_check, force_cancel_check=force_cancel_check,
        on_progress=progress, _resume_state=state,
        archive_tool=config.get('_archive_tool'),output_list=bool(config.get('output_list')),**options)
    success = len(result.records)
    failed = int(result.status=='error' and success<total)
    stats.update(groups=success, groups_success=success, groups_failed=failed,
                 groups_pending=total-success-failed)
    if result.error:
        stats['error_detail'] = result.error
    if result.status=='completed':
        from sortzip_core import engine
        from sortzip_core.restore_bundle import write_bundle, write_category_bundle
        stage = tr('发布产物名称')
        try:
            state = json.loads(engine._checkpoint_path(root,state).read_text(encoding='utf-8'))
            publication = state.get('publication')
            if publication is not None:
                archive_publication.publish(root,plan,state,engine._write_checkpoint,cancel=cancel_check)
                result.records = state['completed']
            stage = tr('生成后缀还原脚本')
            bundles = {}
            category_mappings = {}
            final_ids = set(plan.final_ids)
            final_tasks = {task.unit.unit_id:task for task in plan.tasks if task.unit.unit_id in final_ids}
            for identity,task in final_tasks.items():
                record = result.records[identity]
                if not any(item['shown']!=item['canonical'] for item in record['files']):
                    continue
                folder = root/record['folder']
                if publication is None:
                    kit = write_bundle(folder/'Restore-Archive-Names',[
                        (folder/item['shown'],item['canonical']) for item in record['files']])
                    bundles[identity] = kit.relative_to(root).as_posix()
                else:
                    category = root/Path(task.unit.folder).parts[0]
                    checksums = {row['shown']:row['sha256'] for row in record.get('delivery',{}).get('volumes',())}
                    category_mappings.setdefault(category,[]).extend(
                        (folder/item['shown'],item['canonical'],checksums.get(item['shown'])) for item in record['files']
                        if item['shown']!=item['canonical'])
                    bundles[identity] = archive_publication.restore_paths(category,state)[0].relative_to(root).as_posix()
            for category,mappings in category_mappings.items():
                paths = archive_publication.restore_paths(category,state)
                adjacent = len(paths)==2
                for path in paths:
                    archive_publication.checked_path(root,Path(state['input_root']),path.relative_to(root))
                write_category_bundle(category if adjacent else paths[0],mappings,
                                      language=state.get('script_language'),adjacent=adjacent)
            state['restore_bundles'] = bundles
            if state.get('delivery_manifest'):
                from sortzip_core.delivery_manifest import EXTENSION,write_outer,write_task
                stage = tr('生成自动解包清单')
                if state.get('task_delivery'):
                    write_task(root,plan,state,config)
                else:
                    layer_count = plan.tasks[-1].layer_index+1
                    for identity in plan.final_ids:
                        record = result.records[identity]
                        manifest = record['delivery']
                        path = file_journal.safe_path(root,record['folder']+'/'+manifest['stem']+EXTENSION)
                        write_outer(path,manifest,layer_count)
            if state.get('schema',0)>=3 and state['output_list']:
                stage = tr('生成文件清单')
                stats['file_lists'] = publish_lists(root,plan,result,state)
            else:
                stats['file_lists'] = 0
            stage = tr('写入断点')
            engine._write_checkpoint(root,state)
            if publication is not None:
                archive_publication.remove_staging(root,state)
        except file_journal.Cancelled:
            return engine._cancel_status(force_cancel_check)
        except (OSError,ValueError) as error:
            tool_name = TOOL_NAMES[config.get('archive_tool','bandizip')]
            stats['error_detail'] = tr('{0}「{1}」失败：{2}。可从断点重新完成输出：{3}', tool_name, stage, error, engine._checkpoint_path(root,state))
            print(stats['error_detail'])
            return 'error'
    return result.status


def publish_lists(root,plan,result,state=None):
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
    recorded_paths = {row['source_rel']:root/row['folder']/row['target_name']
                      for row in (state or {}).get('file_plan',())}
    for task in plan.tasks:
        if task.layer_index!=0:
            break
        folder = file_journal.safe_path(root,task.unit.folder)
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError(tr('文件清单目录无效'))
        if folder not in contents:
            contents[folder] = []
            counts[folder] = 0
        for source_id, original in zip(task.input_ids,task.unit.original_ids,strict=True):
            source = recorded_paths.get(original)
            if source is None:
                source = Path(result.units[source_id].files[0])
            if source.parent.resolve()!=folder:
                raise ValueError(tr('文件清单与分类位置不一致'))
            counts[folder] += 1
            contents[folder].append((counts[folder],original,source.name,*outer[original]))
    return write_lists(contents,('序号','原文件名','新文件名','所属压缩包名','标准入口卷名','归档文件数'))


_COMMON_HEADERS = ('序号','原文件名','新文件名','所属压缩包名')
_LIST_HEADERS = (_COMMON_HEADERS[:3],_COMMON_HEADERS,
                 _COMMON_HEADERS+('标准入口卷名','归档文件数'))
_LIST_WIDTHS = (4,24,24,24,24,10)
_COLUMN_GAP = '  '


def _list_widths(header,legacy):
    from sortzip_core.engine import _disp_len
    base = _LIST_WIDTHS[:len(header)]
    if legacy:
        return base
    return tuple(max(width,_disp_len(name)+2) for name,width in zip(header,base,strict=True))


def format_list(header,rows,*,separator=_COLUMN_GAP,legacy=False,widths=None):
    from sortzip_core.engine import _pad_center, _wrap_text
    widths = _list_widths(header,legacy) if widths is None else widths
    def render(cells):
        return separator.join(_pad_center(cell,width,legacy=legacy)
                              for cell,width in zip(cells,widths,strict=True))
    lines = [render(header)]
    for row in rows:
        columns = [_wrap_text(str(cell),width) for cell,width in zip(row,widths,strict=True)]
        for line in zip_longest(*columns,fillvalue=''):
            lines.append(render(line))
    return ('\n'.join(lines)+'\n').encode('utf-8')


def list_rows(path):
    """Read generated correspondence lists for the verification scripts."""
    with path.open(encoding='utf-8-sig',newline='') as stream:
        from sortzip_core.engine import _disp_len
        first = next(stream,'').rstrip('\r\n')
        tabbed = '\t' in first
        if tabbed:
            header = tuple(cell.strip() for cell in first.split('\t'))
            legacy = first==format_list(header,(),separator='\t',legacy=True).decode('utf-8').rstrip('\n')
            if first=='\t'.join(header):
                for cells in csv.reader(stream,delimiter='\t'):
                    yield dict(zip(header,cells,strict=True))
                return
        else:
            for header in reversed(_LIST_HEADERS):
                if all(label in first for label in header):
                    break
            else:
                raise ValueError(tr('文件清单表头无效：{0}',path))
            legacy = first==format_list(header,(),separator=_COLUMN_GAP,legacy=True).decode('utf-8').rstrip('\n')
        widths = _list_widths(header,legacy)

        def split(line):
            if tabbed:
                return [cell.strip() for cell in line.split('\t')]
            cells = []
            start = end = 0
            for width in widths:
                measured = 0
                while end<len(line) and measured<width:
                    measured += _disp_len(line[end])
                    end += 1
                cells.append(line[start:end].strip())
                start = end = end+len(_COLUMN_GAP)
            return cells

        record = None
        for row in stream:
            cells = split(row.rstrip('\r\n'))
            next_index = str(int(record[0])+1) if record is not None else '1'
            if cells[0] and (record is None or cells[0] in (next_index,next_index[:widths[0]])):
                if record is not None:
                    yield dict(zip(header,record,strict=True))
                record = cells
            elif record is not None:
                record = [value+rest for value,rest in zip(record,cells,strict=True)]
        if record is not None:
            yield dict(zip(header,record,strict=True))


def write_lists(contents,header):
    """Return the number of published lists, including identical lists reused on resume."""
    for folder,rows in contents.items():
        path = folder/'List.txt'
        content = format_list(header,rows)
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_file():
                raise ValueError(tr('文件清单位置已占用：{0}', path))
            existing = path.read_bytes().replace(b'\r\n',b'\n').removesuffix(b'\n')
            if existing != content.removesuffix(b'\n'):
                stream = io.StringIO(newline='')
                writer = csv.writer(stream,delimiter='\t',lineterminator='\n')
                writer.writerow(header)
                writer.writerows(rows)
                historical = (stream.getvalue().encode('utf-8'),format_list(header,rows,legacy=True),
                              format_list(header,rows,separator='\t',legacy=True),
                              format_list(header,rows,separator='\t'))
                if all(existing!=old.removesuffix(b'\n') for old in historical):
                    raise ValueError(tr('文件清单位置已占用：{0}', path))
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
    return len(contents)
