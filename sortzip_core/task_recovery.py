"""Start and resume the official workspace task from its first file operation."""
import json
from pathlib import Path
import re
import secrets
import sqlite3
from datetime import datetime
from time import monotonic

from sortzip_core import engine, file_journal as files, nested_pipeline, resume_passwords, retention
from sortzip_core.constants import RESUME_FILE_NAME
from sortzip_core.nested_execution import execute_plan, task_specs
from sortzip_core.nested_resume import load_resume
from sortzip_core.i18n import current_language, tr


def _journal_issue(error):
    if isinstance(error,OverflowError):
        return tr('无法保存文件的恢复记录。')
    if isinstance(error,sqlite3.Error):
        code = getattr(error,'sqlite_errorcode',0) & 0xff
        if code==sqlite3.SQLITE_FULL:
            return tr('磁盘空间不足，无法保存恢复记录。')
        if code in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
            return tr('恢复记录正在被占用，请关闭正在使用它的程序后重试。')
        return tr('无法读取或保存恢复记录。')
    return error


def read_state(path,*,state=None):
    if path.name!=RESUME_FILE_NAME or path.is_symlink():
        raise ValueError(tr('请选择标准断点文件'))
    if state is None:
        state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state,dict) or state.get('schema') not in (4,retention.SCHEMA) or
            state.get('kind') not in ('multilayer','file_task') or
            not isinstance(state.get('job'),str) or not re.fullmatch('[0-9a-f]{32}',state['job']) or
            not isinstance(state.get('file_plan'),list) or not state['file_plan'] or
            any(type(state.get(key)) is not bool for key in ('keep_files','output_list','files_ready','auto_close'))):
        raise ValueError(tr('文件任务断点结构无效'))
    if not state['files_ready'] and state.get('completed'):
        raise ValueError(tr('断点在完成文件操作前记录了归档'))
    if state['kind']=='file_task' and (state.get('tasks')!=[] or state.get('completed')!={} or
                                      state.get('archive_tool') is not None):
        raise ValueError(tr('只分类任务包含无效的归档记录'))
    root = files.recovery_output(path)
    if state.get('managed_recovery',False) is not (root!=path.parent):
        raise ValueError(tr('文件任务断点结构无效'))
    if root!=path.parent:
        if files.compression_directory(root,state)!=path.parent:
            raise ValueError(tr('文件任务断点结构无效'))
    elif 'recovery_folder' in state:
        raise ValueError(tr('文件任务断点结构无效'))
    operations = list(files.steps(root,state))
    expected_sources = {'source:'+entry['source_rel']:{str(step.target):list(step.source_id[2:])}
                        for entry,step in zip(state['file_plan'],operations,strict=True)}
    if state['sources']!=expected_sources:
        raise ValueError(tr('文件操作计划和归档输入不一致'))
    if state['kind']=='multilayer':
        expected_folders = {'source:'+item['source_rel']:item['folder'] for item in state['file_plan']}
        for task in state.get('tasks',[]):
            if task['layer']==0 and any(expected_folders.get(source)!=task['folder'] for source in task['inputs']):
                raise ValueError(tr('分类与归档计划目录不一致'))
    if state['schema']==retention.SCHEMA and (state['kind']!='multilayer'
            or type(state.get('restore_original_names')) is not bool):
        raise ValueError(tr('断点产物保留设置无效'))
    if (any(type(state.get(key,False)) is not bool for key in ('delivery_manifest','delivery_original_names'))
            or state.get('delivery_original_names') and not state.get('delivery_manifest')
            or state.get('delivery_manifest') and state['schema']!=retention.SCHEMA):
        raise ValueError(tr('解包清单结构无效'))
    mode = state.get('delivery_password_mode','none')
    aggregate = state.get('task_delivery',False)
    if (type(aggregate) is not bool or mode not in ('none','plain','encrypted')
            or (aggregate and (not state.get('delivery_manifest') or not state.get('managed_recovery')
                               or state.get('archive_tool') not in ('bandizip','7zip')))
            or (not aggregate and mode!='none')
            or type(state.get('finished',False)) is not bool):
        raise ValueError(tr('解包清单结构无效'))
    if 'delivery_manifest_name' in state:
        from sortzip_core.delivery_manifest import task_manifest_name
        task_manifest_name(state)
    return state,operations


def input_needed(db,operations):
    if files.phase(db)!='classify':
        return False
    recorded = dict(db.execute('SELECT id,step FROM operations'))
    for item in operations:
        step = recorded.get(item.index)
        if step=='classified':
            continue
        if step in ('move','remove') and not files.exists(item.source):
            continue
        return True
    return False


def start(config,stats,on_progress,cancel_check,force_cancel_check):
    started_at = datetime.now()
    root = Path(config['dest']).resolve()
    source_root = Path(config['src']).resolve()
    plan = config.get('_nested_plan')
    job = secrets.token_hex(16)
    entries = []
    file_plan = config['execution_plan']
    next_progress = 0
    for number,entry in enumerate(file_plan,1):
        if engine._check_cancel(cancel_check):
            return engine._cancel_status(force_cancel_check)
        original = files.safe_path(source_root,entry['source_rel'])
        now = monotonic()
        if on_progress and now>=next_progress:
            on_progress(0,0,0,0,tr('正在校验源文件 {0}/{1}：{2}', number, len(file_plan), original.name))
            next_progress = now+0.25
        file_id = files.identity(original)
        if file_id[2:]!=(entry['size'],entry['mtime_ns']):
            raise ValueError(tr('源文件在预检后变化：{0}', original))
        try:
            sha = files.digest(original,cancel_check)
        except files.Cancelled:
            return engine._cancel_status(force_cancel_check)
        if files.identity(original)!=file_id:
            raise ValueError(tr('读取时源文件发生变化：{0}', original))
        entries.append(dict(source_rel=entry['source_rel'],folder=entry['folder'],
            classified_name=entry['classified_name'],target_name=entry['target_name'],
            identity=list(file_id),sha256=sha))
    state = dict(schema=4,kind='multilayer' if plan else 'file_task',job=job,
                 input_root=str(source_root),file_plan=entries,keep_files=bool(config.get('keep_files')),
                 output_list=bool(config.get('output_list')),auto_close=True,
                 files_ready=False,archive_tool=config.get('archive_tool','bandizip') if plan else None,
                 tasks=task_specs(plan) if plan else [],completed={},final_ids=list(plan.final_ids) if plan else [],
                 password_checks=resume_passwords.make_checks(nested_pipeline.prepare_layers(config)) if plan else {})
    if plan:
        from sortzip_core.archive_publication import CATEGORY_FILE_LAYOUT
        state['script_language'] = current_language()
        state['category_restore_layout'] = CATEGORY_FILE_LAYOUT
        state['publication'] = dict(started=False,done=[])
    if config.get('managed_recovery'):
        state['managed_recovery'] = True
    if plan and (config.get('retain_layers') is not None or config.get('delivery_manifest')
                 or config.get('restore_original_names')):
        keep = config.get('retain_layers')
        outer = plan.tasks[-1].layer_index
        state.update(schema=retention.SCHEMA,retain_layers=list(range(outer) if keep is None else keep),cleanup={},
                     restore_original_names=bool(config.get('restore_original_names')))
        if config.get('delivery_manifest'):
            from sortzip_core.delivery_manifest import uses_task_manifest
            state.update(delivery_manifest=True,delivery_original_names=bool(config.get('delivery_original_names')))
            if uses_task_manifest(config):
                state['task_delivery'] = True
                mode = config.get('delivery_password_mode','none')
                if mode not in ('none','plain','encrypted'):
                    raise ValueError(tr('交付密码记录无效。'))
                if mode=='encrypted' and not 12<=len(config.get('delivery_phrase',''))<=1024:
                    raise ValueError(tr('交付口令至少需要 12 个字符。'))
                state['delivery_password_mode'] = mode
        retention.validate(state,plan)
    operations = list(files.steps(root,state))
    state['sources'] = {'source:'+entry['source_rel']:{str(step.target):list(step.source_id[2:])}
                        for entry,step in zip(entries,operations,strict=True)}
    if engine._check_cancel(cancel_check):
        return engine._cancel_status(force_cancel_check)
    root.mkdir(parents=True,exist_ok=True)
    managed = state.get('managed_recovery',False)
    recovery = files.recovery_directory(root) if managed else root
    with files.task_lock(root):
        existing = files.managed_compression(root)
        checkpoint = (existing or root)/RESUME_FILE_NAME
        if existing is not None or files.exists(root/RESUME_FILE_NAME) or files.exists(root/files.DATABASE):
            raise ValueError(tr('输出目录已有文件任务，请先恢复：{0}', checkpoint))
        if managed:
            state['recovery_folder'] = files.choose_compression_directory(
                recovery,(row['folder'] for row in entries),started_at)
        directory = files.compression_directory(root,state) if managed else root
        checkpoint = directory/RESUME_FILE_NAME
        if source_root.is_relative_to(root):
            operations = list(files.steps(root,state))
        for item in operations:
            if files.exists(item.classified) or files.exists(item.copy) or files.exists(item.staged):
                raise ValueError(tr('分类位置已占用：{0}', item.classified))
        occupied = {str(item.classified).casefold() for item in operations}
        for item in operations:
            if files.exists(item.target) and str(item.target).casefold() not in occupied:
                raise ValueError(tr('重命名位置已占用：{0}', item.target))
            listing = item.target.parent/'List.txt'
            if state['output_list'] and (files.exists(listing) or listing.name.casefold() in
                    (item.target.name.casefold(),item.classified.name.casefold())):
                raise ValueError(tr('文件清单位置已占用：{0}', listing))
        if state.get('task_delivery'):
            from sortzip_core.delivery_manifest import choose_task_manifest_name
            final_ids = set(plan.final_ids)
            folders = (task.unit.folder for task in plan.tasks if task.unit.unit_id in final_ids)
            state['delivery_manifest_name'] = choose_task_manifest_name(root,folders,started_at)
        created_directory = False
        try:
            if managed:
                files.ensure_recovery_directory(root)
                directory.mkdir()
                created_directory = True
            engine._write_checkpoint(root,state)
            with files.journal(directory,job,create=True) as db:
                status = _continue(root,config,state,db,operations,stats,on_progress,cancel_check,force_cancel_check)
            return _finish(root,checkpoint,status,stats)
        except (OSError,ValueError,sqlite3.Error,OverflowError) as error:
            if checkpoint.is_file():
                stats['recovery_path'] = str(checkpoint)
            elif created_directory and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                files.remove_empty_recovery(root)
            raise ValueError(tr('文件任务未完成：{0}\n断点位置：{1}', _journal_issue(error), checkpoint)) from error


def resume(config,stats,on_progress,cancel_check,force_cancel_check):
    checkpoint = Path(config['resume_from']).absolute()
    checkpoint = checkpoint.parent.resolve()/checkpoint.name
    root = files.recovery_output(checkpoint)
    directory = checkpoint.parent
    state,operations = read_state(checkpoint)
    if state['kind']=='multilayer' and state.get('finished') is not True:
        plan,_,validated = load_resume(checkpoint,config.get('layer_passwords'))
        config = dict(config,archive_tool=validated['archive_tool'])
        try:
            correct = resume_passwords.verify(root,None,plan,validated,config.get('layer_passwords') or {},
                                              cancel_check,force_cancel_check)
        except engine.ForceCancelled:
            return 'force_cancelled'
        if not correct:
            return engine._cancel_status(force_cancel_check)
        if len(state['completed'])<len(plan.tasks):
            nested_pipeline.preflight_tool(config)
    with files.task_lock(root):
        latest,operations = read_state(checkpoint)
        if latest!=state:
            raise ValueError(tr('断点在检查期间发生变化，请重新载入：{0}', checkpoint))
        try:
            if state.get('finished') is True:
                return _finish(root,checkpoint,'completed',stats)
            create = not files.exists(directory/files.DATABASE)
            if create and state['files_ready']:
                raise ValueError(tr('缺少已产生文件操作的日志'))
            if create:
                _require_input_approval(config,state)
                for item in operations:
                    item.original()
                    if any(files.exists(path) for path in (item.classified,item.target,item.copy,item.staged)):
                        raise ValueError(tr('缺少文件日志且输出已有文件：{0}', item.classified))
            with files.journal(directory,state['job'],create=create) as db:
                if state['files_ready'] and files.phase(db)!='ready':
                    raise ValueError(tr('断点与文件日志阶段不一致'))
                if input_needed(db,operations):
                    _require_input_approval(config,state)
                status = _continue(root,config,state,db,operations,stats,on_progress,cancel_check,force_cancel_check)
            return _finish(root,checkpoint,status,stats)
        except (OSError,ValueError,sqlite3.Error,OverflowError) as error:
            if checkpoint.is_file():
                stats['recovery_path'] = str(checkpoint)
            raise ValueError(tr('文件任务未完成：{0}\n断点位置：{1}', _journal_issue(error), checkpoint)) from error


def _finish(root,checkpoint,status,stats):
    if root==checkpoint.parent:
        return status
    if status!='completed':
        stats['recovery_path'] = str(checkpoint)
        return status
    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    state['finished'] = True
    engine._write_checkpoint(root,state)
    try:
        for suffix in ('-journal','-wal','-shm',''):
            item = files.safe_path(checkpoint.parent,files.DATABASE+suffix)
            if files.exists(item):
                files.identity(item)
                item.unlink()
        unexpected = [item for item in checkpoint.parent.iterdir() if item!=checkpoint]
        if unexpected:
            raise ValueError(tr('输出位置已被占用：{0}',unexpected[0]))
        checkpoint.unlink()
        checkpoint.parent.rmdir()
        files.remove_empty_recovery(root)
    except (OSError,ValueError):
        stats['recovery_path'] = str(checkpoint)
        print(tr('任务已完成，恢复记录尚未清理：{0}',checkpoint.parent))
    return status


def _require_input_approval(config,state):
    approved = config.get('approved_input_root')
    if not isinstance(approved,str) or Path(approved).resolve()!=Path(state['input_root']):
        raise ValueError(tr('请先确认原输入目录：{0}', state["input_root"]))


def _continue(root,config,state,db,operations,stats,on_progress,cancel_check,force_cancel_check):
    if not state['files_ready']:
        if config.get('_archive_tool_identity') is not None:
            nested_pipeline.preflight_tool(config)
        if not files.run(db,operations,state['keep_files'],stats,on_progress,cancel_check):
            return engine._cancel_status(force_cancel_check)
        state['files_ready'] = True
        engine._write_checkpoint(root,state)
    elif state['kind']=='file_task' or len(state['completed'])<len(state['tasks']):
        cleaned = state.get('cleanup',{})
        files.verify_ready(db,operations,skip={item.index for row,item in zip(state['file_plan'],operations,strict=True)
                                               if 'source:'+row['source_rel'] in cleaned})
    if engine._check_cancel(cancel_check):
        return engine._cancel_status(force_cancel_check)
    if state['kind']=='file_task':
        if state['output_list']:
            contents = {}
            counts = {}
            for item,row in zip(operations,state['file_plan'],strict=True):
                folder = item.target.parent
                counts[folder] = counts.get(folder,0)+1
                contents.setdefault(folder,[]).append((counts[folder],row['source_rel'],item.target.name))
            stats['file_lists'] = nested_pipeline.write_lists(contents,('序号','原文件名','新文件名'))
        else:
            stats['file_lists'] = 0
        return 'completed'
    checkpoint = engine._checkpoint_path(root,state)
    passwords = config.get('layer_passwords') or {}
    if not config.get('resume_from'):
        passwords = {index:layer.password for index,layer in enumerate(nested_pipeline.prepare_layers(config))}
    plan,available,validated = load_resume(checkpoint,passwords)
    cleanup = None
    if validated['schema']==retention.SCHEMA:
        preserved = retention.unverified_cleanup(plan,validated)
        if preserved:
            print(tr('旧断点有 {0} 个输入单元未记录归档成员校验。现存文件将保留。',len(preserved)))
        cleanup = retention.Cleanup(root,plan,validated,operations,db,engine._write_checkpoint)
        cleanup.replay(cancel_check)
        if engine._check_cancel(cancel_check):
            return engine._cancel_status(force_cancel_check)
    return nested_pipeline._run(plan,available,root,config,stats,on_progress,cancel_check,
                                force_cancel_check,execute_plan,validated,cleanup=cleanup)
