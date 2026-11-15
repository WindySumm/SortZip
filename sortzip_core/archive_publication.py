"""Publish verified archive groups from task-owned storage without replacing files."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from sortzip_core import file_journal as files
from sortzip_core.i18n import tr
from sortzip_core.restore_bundle import CATEGORY_RESTORE_LOCK, category_launcher_name


KIT = 'Restore-Archive-Names'
CATEGORY_FILE_LAYOUT = 'category-file'
OWNER = '.sortzip-owner'


def staging(root,state):
    base = files.compression_directory(root,state) if state.get('managed_recovery') else root
    return base/('.sortzip-archives-'+state['job'])


def prepare_staging(root,state):
    base = staging(root,state)
    source = Path(state['input_root'])
    checked_path(root,source,base.relative_to(root))
    if not files.exists(base):
        with TemporaryDirectory(prefix='.sortzip-archives-init-',dir=root) as temporary:
            directory = Path(temporary)/base.name
            directory.mkdir()
            with (directory/OWNER).open('xb') as owner:
                owner.write(state['job'].encode('ascii'))
                owner.flush()
                os.fsync(owner.fileno())
            files._sync_dir(directory)
            directory.rename(base)
            files._sync_dir(root)
    verify_owner(base,state)


def verify_owner(base,state):
    if not base.is_dir() or base.is_symlink() or base.is_junction():
        raise ValueError(tr('归档临时目录不属于此任务：{0}',base))
    marker = files.safe_path(base,OWNER)
    if (not files.exists(marker)
            or files.identity(marker)[2]!=32 or marker.read_bytes()!=state['job'].encode('ascii')):
        raise ValueError(tr('归档临时目录不属于此任务：{0}',base))


def folder_for(task,final_ids):
    folder = Path(task.unit.folder)
    return folder if task.unit.unit_id in final_ids else folder/f'layer-{task.layer_index+1}'


def restore_paths(category,state):
    if state.get('category_restore_layout')==CATEGORY_FILE_LAYOUT:
        return (category/category_launcher_name(state.get('script_language')),category/CATEGORY_RESTORE_LOCK)
    return (category/KIT,)


def work_folder(root,task,state=None):
    base = staging(root,state) if state is not None and 'publication' in state else root
    return base/f'layer-{task.layer_index+1}'/task.unit.folder/task.archive_stem


def kept(plan,state):
    retained = state.get('retain_layers',range(plan.tasks[-1].layer_index))
    final_ids = set(plan.final_ids)
    return (task for task in plan.tasks if task.unit.unit_id in final_ids
            or task.layer_index in retained)


def checked_path(root,source,relative):
    path = files.safe_path(root,Path(relative).as_posix())
    if path.is_junction():
        raise ValueError(tr('目录已被替换或为链接：{0}',path))
    if source is not None:
        files.check_output_outside_input(root,source,path)
    return path


def check_publish_device(root,folder):
    if not root.exists():
        return
    current = folder
    while not files.exists(current):
        current = current.parent
    if current.stat().st_dev!=root.stat().st_dev:
        raise ValueError(tr('压缩包的输出位置必须与任务目录位于同一磁盘：{0}',folder))


def check_destinations(root,source,targets,directory_paths,occupied=(),*,context=None):
    for folder in directory_paths:
        if context: context.check()
        checked_path(root,source,folder.relative_to(root))
        if files.exists(folder) and not folder.is_dir():
            raise ValueError(tr('输出位置已被占用：{0}',folder))
        check_publish_device(root,folder)
    directories = {str(path).casefold() for parent in directory_paths for path in (parent,*parent.parents)}
    seen = {str(path).casefold() for path in occupied}
    for path in occupied:
        if str(path).casefold() in directories:
            raise ValueError(tr('文件和文件夹会使用同一位置：{0}',path))
    planned = {str(path).casefold() for path in targets}
    for path in targets:
        if context: context.check()
        key = str(path).casefold()
        if key in seen or key in directories or files.exists(path):
            raise ValueError(tr('输出位置已被占用：{0}',path))
        if any(str(parent).casefold() in seen or str(parent).casefold() in planned for parent in path.parents):
            raise ValueError(tr('文件和文件夹会使用同一位置：{0}',path))
        checked_path(root,source,path.relative_to(root))
        seen.add(key)


def current_volume(original,target,expected,*,allow_alternate):
    present = files.exists(original)
    alternate = allow_alternate and files.exists(target)
    if present and alternate:
        if os.name=='nt' or files.identity(original)!=expected or files.identity(target)!=expected:
            raise ValueError(tr('输出位置已被占用：{0}',target))
    actual = original if present else target if alternate else original
    if not files.exists(actual) or files.identity(actual)!=expected:
        raise ValueError(tr('已完成产物缺失或发生变化：{0}',actual))
    return actual


def validate_state(plan,state):
    if 'category_restore_layout' in state and (
            state['category_restore_layout']!=CATEGORY_FILE_LAYOUT or 'publication' not in state
            or state.get('script_language') not in ('zh','en')):
        raise ValueError(tr('断点产物发布记录无效'))
    if 'publication' not in state:
        return
    publication = state['publication']
    if (state['schema'] not in (4,5) or not isinstance(publication,dict) or set(publication)!={'started','done'}
            or type(publication['started']) is not bool or not isinstance(publication['done'],list)
            or not isinstance(state.get('job'),str)
            or len(state['job'])!=32 or any(c not in '0123456789abcdef' for c in state['job'])):
        raise ValueError(tr('断点产物发布记录无效'))
    order = [task.unit.unit_id for task in kept(plan,state)]
    done = publication['done']
    if (done!=order[:len(done)] or (done and not publication['started'])
            or publication['started'] and len(state['completed'])!=len(plan.tasks)):
        raise ValueError(tr('断点产物发布记录无效'))
    if publication['started'] and state['schema']==5:
        from sortzip_core.retention import eligible
        consumers = eligible(plan,state['retain_layers'])
        if any(not isinstance(state['completed'].get(consumer),dict)
               or state['completed'][consumer].get('members_verified') is not True
               or state['cleanup'].get(unit)!='done' for unit,consumer in consumers.items()):
            raise ValueError(tr('断点产物发布记录无效'))


def publish(root,plan,state,save,*,cancel=None):
    progress = state['publication']
    source = Path(state['input_root'])
    groups = list(kept(plan,state))
    records = state['completed']
    final_ids = set(plan.final_ids)
    done = set(progress['done'])
    pending = [(task,root/folder_for(task,final_ids)) for task in groups
               if task.unit.unit_id not in done]
    if pending:
        verify_owner(staging(root,state),state)
    if not progress['started']:
        volume_paths = []
        auxiliary = set()
        for task,folder in pending:
            key = task.unit.unit_id
            record = records[key]
            for row in record['files']:
                volume_paths.append(folder/row['shown'])
                if row['shown'].casefold()!=row['canonical'].casefold():
                    volume_paths.append(folder/row['canonical'])
            if key in final_ids:
                if state.get('delivery_manifest') and not state.get('task_delivery'):
                    auxiliary.add(folder/(task.archive_stem+'.sortzip.json'))
                if any(row['shown']!=row['canonical'] for row in record['files']):
                    auxiliary.update(restore_paths(root/Path(task.unit.folder).parts[0],state))
        if state['output_list']:
            auxiliary.update(root/task.unit.folder/'List.txt' for task in plan.tasks if not task.layer_index)
        if state.get('task_delivery'):
            from sortzip_core.delivery_manifest import task_manifest_name
            auxiliary.add(root/task_manifest_name(state))
        occupied = [root/row['folder']/row[name] for row in state['file_plan']
                    for name in ('target_name','classified_name')]
        check_destinations(root,source,[*volume_paths,*sorted(auxiliary)],
                           {folder for _,folder in pending},occupied)
        progress['started'] = True
        save(root,state)
    for task,folder in pending:
        check_publish_device(root,folder)
        key = task.unit.unit_id
        record = records[key]
        for row in record['files']:
            if cancel and cancel():
                raise files.Cancelled()
            name = row['shown']
            original = checked_path(root,source,Path(record['folder'])/name)
            target = checked_path(root,source,folder.relative_to(root)/name)
            if name.casefold()!=row['canonical'].casefold():
                standard = checked_path(root,source,folder.relative_to(root)/row['canonical'])
                if files.exists(standard):
                    raise ValueError(tr('输出位置已被占用：{0}',standard))
            expected = tuple(record['identities'][name])
            actual = current_volume(original,target,expected,allow_alternate=True)
            if actual==original:
                target.parent.mkdir(parents=True,exist_ok=True)
                if files.exists(target):
                    # A POSIX link/unlink move can leave both names on interruption.
                    current_volume(original,target,expected,allow_alternate=True)
                    original.unlink()
                    files._sync_dir(original.parent)
                else:
                    files.rename_new(original,target)
                if files.identity(target)!=expected:
                    raise ValueError(tr('已完成产物缺失或发生变化：{0}',target))
        records[key] = dict(record,folder=folder.relative_to(root).as_posix())
        progress['done'].append(key)
        save(root,state)


def remove_staging(root,state):
    # An interrupted compressor can leave partial archives in earlier attempts.
    base = staging(root,state)
    if not files.exists(base):
        return
    checked_path(root,Path(state['input_root']),base.relative_to(root))
    if base.is_dir() and not any(base.iterdir()):
        # The last removal may have stopped just after unlinking the owner marker.
        base.rmdir()
        return
    verify_owner(base,state)
    def remove(directory):
        for child in directory.iterdir():
            if child==base/OWNER:
                continue
            checked_path(root,None,child.relative_to(root))
            if child.is_dir() and not child.is_junction():
                remove(child)
            else:
                files.identity(child)
                child.unlink()
        if directory==base:
            (base/OWNER).unlink()
        directory.rmdir()
    remove(base)
