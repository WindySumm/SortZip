"""Durable operations before compression. Each rename is bracketed by commits."""
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from threading import Lock

from sortzip_core.constants import RESUME_FILE_NAME, is_task_artifact, validate_win_folder_name
from sortzip_core.i18n import tr


DATABASE = '.sortzip-files.sqlite3'
LOCK = '.sortzip-task.lock'
RECOVERY = '.sortzip-recovery'
COMPRESSION = 'compression'
_RECOVERY_OWNER = '.sortzip-recovery-owner'
_active_mutexes = set()
_active_mutexes_lock = Lock()


def recovery_directory(root):
    path = safe_path(root,RECOVERY)
    if path.is_junction() or (exists(path) and not path.is_dir()):
        raise ValueError(tr('目录已被替换或为链接：{0}',path))
    return path


def recovery_output(checkpoint):
    directory = checkpoint.parent
    if is_compression_directory(directory.name) and directory.parent.name==RECOVERY:
        root = directory.parent.parent
        safe_path(root,f'{RECOVERY}/{directory.name}/{checkpoint.name}')
        return root
    return directory


def is_compression_directory(value):
    return isinstance(value,str) and (value==COMPRESSION or bool(
        re.fullmatch(r'compression-\d{4}-\d{2}-\d{2}_\d{6}-.+',value)) and not validate_win_folder_name(value))


def recovery_label(value):
    return value[:32].strip(' .') or 'SortZip'


def compression_directory(root,state):
    name = state.get('recovery_folder',COMPRESSION)
    if not is_compression_directory(name):
        raise ValueError(tr('文件任务断点结构无效'))
    path = safe_path(root,f'{RECOVERY}/{name}')
    if path.is_junction() or (exists(path) and not path.is_dir()):
        raise ValueError(tr('目录已被替换或为链接：{0}',path))
    return path


def managed_compression(root):
    """Locate the sole resumable file task, including the original fixed folder."""
    recovery = recovery_directory(root)
    if not recovery.is_dir():
        return None
    candidates = [safe_path(root,f'{RECOVERY}/{item.name}/{RESUME_FILE_NAME}').parent
                  for item in recovery.iterdir() if is_compression_directory(item.name) and
                  (exists(item/RESUME_FILE_NAME) or exists(item/DATABASE))]
    if len(candidates)>1:
        raise ValueError(tr('输出目录有多个断点，请选择要恢复的文件：{0}',recovery))
    return candidates[0] if candidates else None


def choose_compression_directory(recovery,folders,started):
    from itertools import count
    categories = {}
    for folder in folders:
        label = Path(folder).parts[0]
        categories.setdefault(label.casefold(),label)
    label = recovery_label(next(iter(categories.values())))
    if len(categories)>1:
        label += f'等{len(categories)}类'
    stem = f'{COMPRESSION}-{started:%Y-%m-%d_%H%M%S}-{label}'
    occupied = {item.name.casefold() for item in recovery.iterdir()} if recovery.is_dir() else set()
    for index in count(1):
        candidate = stem if index==1 else f'{stem}-{index}'
        if candidate.casefold() not in occupied:
            return candidate


SQLITE_MAX_INTEGER = (1 << 63) - 1


def identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise ValueError(tr('文件类型无效：{0}', path))
    if not info.st_ino:
        raise ValueError(tr('文件系统无法识别文件身份：{0}', path))
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)


def exists(path):
    return os.path.lexists(path)


def check_output_outside_input(root,source_root,path):
    """Keep generated paths outside the input tree when writing to its parent."""
    if source_root.is_relative_to(root) and path.resolve().is_relative_to(source_root):
        raise ValueError(tr('结果会写进输入文件夹：{0}\n请修改分类名称或另选输出文件夹。',path))


def safe_path(root,relative):
    if not isinstance(relative,str) or '\\' in relative or any(
            validate_win_folder_name(part) for part in relative.split('/')):
        raise ValueError(tr('断点包含无效的相对路径'))
    path = root/relative
    for parent in reversed(path.parents):
        if not parent.is_relative_to(root) or parent==root or not exists(parent):
            continue
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError(tr('目录已被替换或为链接：{0}', parent))
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError(tr('路径越界或为链接：{0}', path))
    return path


def validate_parent_output(root,source_root,entries,output_list,*,context=None):
    """Check planned output files before confirmation or file operations."""
    folders = {}
    paths = {}
    for entry in entries:
        if context: context.check()
        folder = entry['folder']
        if folder not in folders:
            directory = safe_path(root,folder)
            check_output_outside_input(root,source_root,directory)
            if exists(directory) and not directory.is_dir():
                raise ValueError(tr('输出位置已被占用：{0}',directory))
            folders[folder] = directory
        directory = folders[folder]
        for name in (entry['classified_name'],entry['target_name']):
            path = safe_path(root,folder+'/'+name)
            if exists(path):
                raise ValueError(tr('输出位置已被占用：{0}',path))
            paths[str(path).casefold()] = path
    if output_list:
        for directory in folders.values():
            listing = directory/'List.txt'
            if exists(listing):
                raise ValueError(tr('输出位置已被占用：{0}',listing))
            paths[str(listing).casefold()] = listing
    for directory in folders.values():
        for path in (directory,*directory.parents):
            occupied = paths.get(str(path).casefold())
            if occupied is not None:
                raise ValueError(tr('文件和文件夹会使用同一位置：{0}',occupied))


@contextmanager
def _file_lock(root, *, existing=False):
    path = safe_path(root,LOCK)
    if existing and not exists(path):
        yield
        return
    if exists(path):
        identity(path)
    with path.open('r+b' if existing else 'a+b') as stream:
        try:
            if os.name=='nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(tr('输出目录正在执行另一任务：{0}', root)) from error
        yield


def _windows_directory_key(root):
    import ctypes
    from ctypes import wintypes
    drive_type = ctypes.WinDLL('kernel32').GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    # Use the shared file lock for UNC and mapped network drives.
    anchor = root.anchor
    if anchor.startswith('\\\\?\\'):
        if anchor[4:8].casefold()=='unc\\':
            return None
        if anchor[5:6]==':':
            anchor = anchor[4:]
    elif anchor.startswith('\\\\'):
        return None
    if drive_type(anchor) not in (2,3,6):
        return None
    info = root.stat()
    if not info.st_ino or not info.st_dev:
        return None
    return f'{info.st_dev:x}-{info.st_ino:x}'


@contextmanager
def _windows_mutex(root,key):
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateMutexW.argtypes = [wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    for function in (api.ReleaseMutex,api.CloseHandle):
        function.argtypes = [wintypes.HANDLE]
        function.restype = wintypes.BOOL
    with _active_mutexes_lock:
        if key in _active_mutexes:
            raise ValueError(tr('输出目录正在执行另一任务：{0}',root))
        _active_mutexes.add(key)
    try:
        handle = api.CreateMutexW(None,False,'Global\\SortZip.Output.'+key)
        if not handle:
            raise OSError(tr('无法锁定输出目录：{0}（Windows 错误 {1}）',root,ctypes.get_last_error()))
        try:
            result = api.WaitForSingleObject(handle,0)
            if result == 0x102:  # WAIT_TIMEOUT
                raise ValueError(tr('输出目录正在执行另一任务：{0}',root))
            if result not in (0,0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
                raise OSError(tr('无法锁定输出目录：{0}（Windows 错误 {1}）',root,ctypes.get_last_error()))
            try:
                yield
            finally:
                api.ReleaseMutex(handle)
        finally:
            api.CloseHandle(handle)
    finally:
        with _active_mutexes_lock:
            _active_mutexes.remove(key)


@contextmanager
def task_lock(root):
    root = root.resolve(strict=True)
    key = _windows_directory_key(root) if os.name=='nt' else None
    with ExitStack() as stack:
        if key is None:
            stack.enter_context(_file_lock(root))
        else:
            stack.enter_context(_windows_mutex(root,key))
            stack.enter_context(_file_lock(root,existing=True))
        recovery = recovery_directory(root)
        if recovery.is_dir():
            stack.enter_context(_file_lock(recovery,existing=True))
        yield


def legacy_workspace_lock(root):
    """Honor locks left by older unpackers under the workspace directory."""
    return _file_lock(root,existing=True)


def ensure_recovery_directory(root):
    path = recovery_directory(root)
    try:
        path.mkdir()
    except FileExistsError:
        return path
    with (path/_RECOVERY_OWNER).open('xb') as marker:
        marker.write(b'SortZip recovery directory\n')
    return path


def remove_empty_recovery(root):
    path = recovery_directory(root)
    marker = path/_RECOVERY_OWNER
    if not exists(marker) or set(path.iterdir())!={marker}:
        return
    identity(marker)
    if marker.read_bytes()!=b'SortZip recovery directory\n':
        return
    marker.unlink()
    try:
        path.rmdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY,errno.EEXIST) and getattr(error,'winerror',None)!=145:
            raise


@contextmanager
def journal(root,job,create=False,readonly=False):
    path = safe_path(root,DATABASE)
    if create:
        descriptor,name = tempfile.mkstemp(prefix='.sortzip-journal-',dir=root)
        os.close(descriptor)
        initial = Path(name)
        try:
            with closing(sqlite3.connect(initial)) as db:
                db.execute('PRAGMA synchronous=FULL')
                with db:
                    db.execute('CREATE TABLE task (job TEXT, phase TEXT)')
                    db.execute('INSERT INTO task VALUES (?, ?)',(job,'classify'))
                    db.execute('CREATE TABLE operations (id INTEGER PRIMARY KEY, step TEXT, device INTEGER, inode INTEGER)')
            rename_new(initial,path)
        finally:
            initial.unlink(missing_ok=True)
    identity(path)
    for suffix in ('-journal','-wal','-shm'):
        sidecar = safe_path(root,DATABASE+suffix)
        if exists(sidecar):
            identity(sidecar)
    mode = 'ro' if readonly else 'rw'
    with closing(sqlite3.connect(path.as_uri()+f'?mode={mode}',uri=True,timeout=1)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if not readonly:
            db.execute('PRAGMA synchronous=FULL')
        record = db.execute('SELECT job,phase FROM task').fetchall()
        if len(record)!=1 or record[0][0]!=job or record[0][1] not in ('classify','stage','commit','ready'):
            raise ValueError(tr('文件日志与断点不匹配'))
        yield db


def phase(db):
    return db.execute('SELECT phase FROM task').fetchone()[0]


def set_phase(db,value):
    with db:
        db.execute('UPDATE task SET phase=?',(value,))


def record(db,index,step,file_id=(None,None)):
    # The prefix prevents INTEGER affinity from converting large values to lossy REALs.
    stored = tuple(f'id:{value:x}' if value is not None and value>SQLITE_MAX_INTEGER else value
                   for value in file_id)
    with db:
        db.execute('INSERT OR REPLACE INTO operations VALUES (?,?,?,?)',(index,step,*stored))


def _read_file_id(value):
    if value is None or (type(value) is int and value>=0):
        return value
    if isinstance(value,str) and re.fullmatch(r'id:[1-9a-f][0-9a-f]*',value):
        number = int(value[3:],16)
        if number>SQLITE_MAX_INTEGER:
            return number
    raise ValueError(tr('恢复记录中的文件编号无效。'))


def _operation_states(db):
    return {index:(step,(_read_file_id(device),_read_file_id(inode))) for index,step,device,inode in
            db.execute('SELECT id,step,device,inode FROM operations')}


class Cancelled(Exception):
    pass


def digest(path,cancelled=None):
    with path.open('rb') as stream:
        if cancelled is None:
            return hashlib.file_digest(stream,'sha256').hexdigest()
        result = hashlib.sha256()
        while block := stream.read(1024*1024):
            if cancelled():
                raise Cancelled()
            result.update(block)
        return result.hexdigest()


def _windows_move(source,target,*,replace=False):
    import ctypes
    from ctypes import wintypes
    move = ctypes.WinDLL('kernel32',use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]
    move.restype = wintypes.BOOL
    if not move(str(source),str(target),0x8 | (0x1 if replace else 0)):
        code = ctypes.get_last_error()
        if code==17:
            raise OSError(errno.EXDEV,tr('跨盘移动需要先复制'),str(source))
        raise ctypes.WinError(code)


def replace_durable(source,target):
    if os.name=='nt':
        _windows_move(source,target,replace=True)
    else:
        source.replace(target)
        _sync_dir(target.parent)


def rename_new(source,target):
    """Publish without overwriting an existing name."""
    if os.name=='nt':
        _windows_move(source,target)
    else:
        os.link(source,target)
        _sync_dir(target.parent)
        source.unlink()
        _sync_dir(source.parent)


def _sync_dir(path):
    if os.name!='nt':
        descriptor = os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def reconcile_rename(item,source,target,file_id):
    if exists(source):
        item.check(source,file_id)
        if exists(target):
            # The POSIX link/unlink implementation can leave both names
            # referring to the same inode when interrupted.
            if os.name=='nt' or identity(target)[:2]!=file_id:
                raise ValueError(tr('操作位置已占用：{0}', target))
            item.check(target,file_id)
            source.unlink()
            _sync_dir(source.parent)
        else:
            rename_new(source,target)
    item.check(target,file_id)


@dataclass(frozen=True)
class FileStep:
    index: int
    source: Path
    classified: Path
    target: Path
    copy: Path
    staged: Path
    source_id: tuple
    sha256: str

    def check(self,path,file_id):
        expected = (*file_id,*self.source_id[2:])
        if not exists(path) or identity(path)!=expected or digest(path)!=self.sha256 or identity(path)!=expected:
            raise ValueError(tr('文件缺失、被替换或内容已变化：{0}', path))

    def original(self):
        self.check(self.source,self.source_id[:2])


def _row_paths(root,source_root,job,item,index):
    original = safe_path(source_root,item['source_rel'])
    classified = safe_path(root,item['folder']+'/'+item['classified_name'])
    target = safe_path(root,item['folder']+'/'+item['target_name'])
    copy = safe_path(root,item['folder']+f'/.sortzip-{job}-{index}.copy')
    staged = safe_path(root,item['folder']+f'/.sortzip-{job}-{index}.rename')
    if is_task_artifact(classified.name) or is_task_artifact(target.name):
        raise ValueError(tr('文件名与任务日志冲突'))
    return original,classified,target,copy,staged


def steps(root,state):
    raw_root = state['input_root']
    if not isinstance(raw_root,str) or not Path(raw_root).is_absolute():
        raise ValueError(tr('断点包含无效的输入目录'))
    source_root = Path(raw_root)
    if source_root.resolve()!=source_root or source_root==root:
        raise ValueError(tr('恢复输入目录无效或已被替换'))
    groups = [set(),set(),set()]
    scratch = set()
    for index,item in enumerate(state['file_plan']):
        if not isinstance(item,dict) or not isinstance(item.get('folder'),str):
            raise ValueError(tr('断点文件记录无效'))
        for key in ('classified_name','target_name'):
            if not isinstance(item.get(key),str) or validate_win_folder_name(item[key]):
                raise ValueError(tr('断点文件名无效'))
        paths = _row_paths(root,source_root,state['job'],item,index)
        for group,path in zip(groups,paths[:3]):
            key = str(path).casefold()
            if key in group:
                raise ValueError(tr('断点文件路径重复：{0}', path))
            group.add(key)
        scratch.update(str(path).casefold() for path in paths[3:])
        if paths[0].is_relative_to(root) and root.is_relative_to(source_root):
            raise ValueError(tr('源文件在输出文件夹内：{0}',paths[0]))
        check_output_outside_input(root,source_root,paths[1].parent)
        source_id = item['identity']
        sha = item['sha256']
        if (not isinstance(source_id,list) or len(source_id)!=4 or
                any(type(value) is not int or value<0 for value in source_id) or
                not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError(tr('断点文件身份无效'))
        yield FileStep(index,*paths,tuple(source_id),sha)
    overlap = groups[0] & (groups[1]|groups[2]|scratch)
    if overlap:
        raise ValueError(tr('结果会占用源文件的位置：{0}',min(overlap)))
    destinations = groups[1]|groups[2]
    if scratch & destinations:
        raise ValueError(tr('断点的暂存路径与输出位置重叠'))
    for path in (*destinations,*scratch):
        if any(str(parent) in destinations for parent in Path(path).parents):
            raise ValueError(tr('断点的输出文件与目录位置重叠'))


def _classify(db,item,keep,step,file_id):
    source, target = item.source,item.classified
    if step is None:
        item.original()
        if exists(target) or exists(item.copy):
            raise ValueError(tr('分类位置已占用：{0}', target))
        target.parent.mkdir(parents=True,exist_ok=True)
        same_device = identity(source)[0]==target.parent.stat().st_dev
        step = 'move' if same_device and not keep else 'copy'
        file_id = item.source_id[:2] if step=='move' else (None,None)
        record(db,item.index,step,file_id)
    if step=='move':
        try:
            reconcile_rename(item,source,target,file_id)
        except OSError as error:
            if error.errno!=errno.EXDEV:
                raise
            step,file_id = 'copy',(None,None)
            record(db,item.index,step)
        if step=='move':
            record(db,item.index,'classified',file_id)
            return
    if step=='copy':
        from sortzip_core.disk_space import check_write
        item.original()
        if exists(target):
            raise ValueError(tr('分类位置已占用：{0}', target))
        if file_id==(None,None):
            # A crash between creating and identifying a temporary file leaves
            # an ambiguous file. Preserve it and stop, rather than truncate it.
            if exists(item.copy):
                raise ValueError(tr('复制暂存位置已占用：{0}', item.copy))
            check_write(target,item.source_id[2],tr('分类复制'))
            with item.copy.open('xb') as output:
                info = os.fstat(output.fileno())
                file_id = (info.st_dev,info.st_ino)
            record(db,item.index,'copy',file_id)
        if not exists(item.copy) or identity(item.copy)[:2]!=file_id:
            raise ValueError(tr('复制暂存文件已被替换：{0}', item.copy))
        if os.name=='nt':
            item.copy.chmod(stat.S_IWRITE)
        with source.open('rb') as incoming,item.copy.open('r+b') as output:
            if (os.fstat(output.fileno()).st_dev,os.fstat(output.fileno()).st_ino)!=file_id:
                raise ValueError(tr('复制暂存文件已被替换：{0}', item.copy))
            output.truncate(0)
            check_write(target,item.source_id[2],tr('分类复制'))
            shutil.copyfileobj(incoming,output)
            output.flush()
            shutil.copystat(source,item.copy)
            os.fsync(output.fileno())
        item.original()
        item.check(item.copy,file_id)
        record(db,item.index,'publish',file_id)
        step = 'publish'
    if step=='publish':
        reconcile_rename(item,item.copy,target,file_id)
        step = 'classified' if keep else 'remove'
        record(db,item.index,step,file_id)
    if step=='remove':
        item.check(target,file_id)
        if exists(source):
            item.original()
            source.unlink()
            _sync_dir(source.parent)
        record(db,item.index,'classified',file_id)
    elif step=='classified':
        item.check(target,file_id)


def _rename(db,item,step,file_id,stage):
    if item.classified.name==item.target.name:
        item.check(item.target,file_id)
        record(db,item.index,'done',file_id)
        return
    source,target = (item.classified,item.staged) if stage else (item.staged,item.target)
    initial,intent,finished = ('classified','staging','staged') if stage else ('staged','committing','done')
    if step==initial:
        item.check(source,file_id)
        if exists(target):
            raise ValueError(tr('改名位置已占用：{0}', target))
        record(db,item.index,intent,file_id)
        step = intent
    if step==intent:
        reconcile_rename(item,source,target,file_id)
        record(db,item.index,finished,file_id)
    else:
        item.check(target,file_id)


def ready_identities(db,files):
    recorded = _operation_states(db)
    if phase(db)!='ready' or len(recorded)!=len(files):
        raise ValueError(tr('文件日志尚未完成'))
    for item in files:
        step,file_id = recorded.get(item.index,(None,None))
        if step!='done':
            raise ValueError(tr('文件日志完成状态无效'))
    return {index:file_id for index,(_,file_id) in recorded.items()}


def verify_ready(db,files,*,skip=()):
    identities = ready_identities(db,files)
    for item in files:
        if item.index not in skip:
            item.check(item.target,identities[item.index])


def run(db,files,keep,stats,on_progress,cancelled):
    states = _operation_states(db)
    if not states.keys() <= set(range(len(files))):
        raise ValueError(tr('操作日志包含未知文件'))
    current = phase(db)
    phases = ('classify','stage','commit','ready')
    valid = (
        {None,'move','copy','publish','remove','classified'},
        {'classified','staging','staged','done'},
        {'staged','committing','done'}, {'done'})
    if any(states.get(item.index,(None,None))[0] not in valid[phases.index(current)] for item in files):
        raise ValueError(tr('操作日志阶段不一致'))
    for index in range(phases.index(current),3):
        for position,item in enumerate(files,1):
            if cancelled and cancelled():
                return False
            step,file_id = states.get(item.index,(None,(None,None)))
            if index==0:
                _classify(db,item,keep,step,file_id)
                stats['files_moved'] = position
                if on_progress:
                    on_progress(0,30,position,len(files),tr('分类：{0}', item.classified.name))
            elif index==1:
                if step=='done':
                    item.check(item.target,file_id)
                else:
                    _rename(db,item,step,file_id,True)
            else:
                _rename(db,item,step,file_id,False)
                if on_progress:
                    on_progress(30,40,position,len(files),tr('重命名：{0}', item.target.name))
        set_phase(db,phases[index+1])
        states = _operation_states(db)
    stats['files_moved'] = len(files)
    stats['files_renamed'] = sum(item.classified.name!=item.target.name for item in files)
    verify_ready(db,files)
    return True
