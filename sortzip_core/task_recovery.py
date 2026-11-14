"""Start and resume the official workspace task from its first file operation."""
import json
from pathlib import Path
import re
import secrets
import sqlite3
from time import monotonic

from sortzip_core import engine, file_journal as files, nested_pipeline, resume_passwords
from sortzip_core.constants import RESUME_FILE_NAME
from sortzip_core.nested_execution import execute_plan, task_specs
from sortzip_core.nested_resume import load_resume
from sortzip_core.i18n import tr


def read_state(path,*,state=None):
    if path.name!=RESUME_FILE_NAME or path.is_symlink():
        raise ValueError(tr('请选择标准断点文件'))
    if state is None:
        state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state,dict) or state.get('schema')!=4 or
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
    operations = list(files.steps(path.parent,state))
    expected_sources = {'source:'+entry['source_rel']:{str(step.target):list(step.source_id[2:])}
                        for entry,step in zip(state['file_plan'],operations,strict=True)}
    if state['sources']!=expected_sources:
        raise ValueError(tr('文件操作计划和归档输入不一致'))
    if state['kind']=='multilayer':
        expected_folders = {'source:'+item['source_rel']:item['folder'] for item in state['file_plan']}
        for task in state.get('tasks',[]):
            if task['layer']==0 and any(expected_folders.get(source)!=task['folder'] for source in task['inputs']):
                raise ValueError(tr('分类与归档计划目录不一致'))
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
    operations = list(files.steps(root,state))
    state['sources'] = {'source:'+entry['source_rel']:{str(step.target):list(step.source_id[2:])}
                        for entry,step in zip(entries,operations,strict=True)}
    if engine._check_cancel(cancel_check):
        return engine._cancel_status(force_cancel_check)
    root.mkdir(parents=True,exist_ok=True)
    checkpoint = engine._checkpoint_path(root)
    with files.task_lock(root):
        if files.exists(checkpoint) or files.exists(root/files.DATABASE):
            raise ValueError(tr('输出目录已有文件任务，请先恢复：{0}', checkpoint))
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
        try:
            engine._write_checkpoint(root,state)
            with files.journal(root,job,create=True) as db:
                return _continue(root,config,state,db,operations,stats,on_progress,cancel_check,force_cancel_check)
        except (OSError,ValueError,sqlite3.Error) as error:
            raise ValueError(tr('文件任务未完成：{0}。断点位置：{1}', error, checkpoint)) from error


def resume(config,stats,on_progress,cancel_check,force_cancel_check):
    checkpoint = Path(config['resume_from']).absolute()
    root = checkpoint.parent.resolve()
    checkpoint = root/checkpoint.name
    state,operations = read_state(checkpoint)
    if state['kind']=='multilayer':
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
        latest,_ = read_state(checkpoint)
        if latest!=state:
            raise ValueError(tr('断点在检查期间发生变化，请重新载入：{0}', checkpoint))
        try:
            create = not files.exists(root/files.DATABASE)
            if create and state['files_ready']:
                raise ValueError(tr('缺少已产生文件操作的日志'))
            if create:
                _require_input_approval(config,state)
                for item in operations:
                    item.original()
                    if any(files.exists(path) for path in (item.classified,item.target,item.copy,item.staged)):
                        raise ValueError(tr('缺少文件日志且输出已有文件：{0}', item.classified))
            with files.journal(root,state['job'],create=create) as db:
                if state['files_ready'] and files.phase(db)!='ready':
                    raise ValueError(tr('断点与文件日志阶段不一致'))
                if input_needed(db,operations):
                    _require_input_approval(config,state)
                return _continue(root,config,state,db,operations,stats,on_progress,cancel_check,force_cancel_check)
        except (OSError,ValueError,sqlite3.Error) as error:
            raise ValueError(tr('文件任务未完成：{0}。断点：{1}', error, checkpoint)) from error


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
        files.verify_ready(db,operations)
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
            nested_pipeline.write_lists(contents,('序号','原文件名','新文件名'))
        return 'completed'
    checkpoint = engine._checkpoint_path(root)
    passwords = config.get('layer_passwords') or {}
    if not config.get('resume_from'):
        passwords = {index:layer.password for index,layer in enumerate(nested_pipeline.prepare_layers(config))}
    plan,available,validated = load_resume(checkpoint,passwords)
    return nested_pipeline._run(plan,available,root,config,stats,on_progress,cancel_check,
                                force_cancel_check,execute_plan,validated)
