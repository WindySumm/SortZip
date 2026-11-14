"""Durable operations before compression. Each rename is bracketed by commits."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from sortzip_core.constants import is_task_artifact, validate_win_folder_name
from sortzip_core.i18n import tr


DATABASE = '.sortzip-files.sqlite3'
LOCK = '.sortzip-task.lock'


def identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise ValueError(tr('文件类型无效：{0}', path))
    if not info.st_ino:
        raise ValueError(tr('文件系统无法识别文件身份：{0}', path))
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)


def exists(path):
    return os.path.lexists(path)


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


@contextmanager
def task_lock(root):
    path = safe_path(root,LOCK)
    if exists(path):
        identity(path)
    with path.open('a+b') as stream:
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
    # Closing the handle releases the lock, including after process termination.


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
    with db:
        db.execute('INSERT OR REPLACE INTO operations VALUES (?,?,?,?)',(index,step,*file_id))


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


def rename_new(source,target):
    """Publish without overwriting an existing name."""
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32',use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]
        move.restype = wintypes.BOOL
        # Write through and never replace or implicitly copy across volumes.
        if not move(str(source),str(target),0x8):
            code = ctypes.get_last_error()
            if code==17:
                raise OSError(errno.EXDEV,tr('跨盘移动需要先复制'),str(source))
            raise ctypes.WinError(code)
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
        if paths[0].is_relative_to(root) or paths[0] in paths[1:]:
            raise ValueError(tr('断点的输入与输出位置重叠'))
        source_id = item['identity']
        sha = item['sha256']
        if (not isinstance(source_id,list) or len(source_id)!=4 or
                any(type(value) is not int or value<0 for value in source_id) or
                not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha)):
            raise ValueError(tr('断点文件身份无效'))
        yield FileStep(index,*paths,tuple(source_id),sha)
    if groups[0] & (groups[1]|groups[2]):
        raise ValueError(tr('断点的输入与输出位置重叠'))
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
        item.original()
        if exists(target):
            raise ValueError(tr('分类位置已占用：{0}', target))
        if file_id==(None,None):
            # A crash between creating and identifying a temporary file leaves
            # an ambiguous file. Preserve it and stop, rather than truncate it.
            if exists(item.copy):
                raise ValueError(tr('复制暂存位置已占用：{0}', item.copy))
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


def verify_ready(db,files):
    recorded = {index:(step,(device,inode)) for index,step,device,inode in
                db.execute('SELECT id,step,device,inode FROM operations')}
    if phase(db)!='ready' or len(recorded)!=len(files):
        raise ValueError(tr('文件日志尚未完成'))
    for item in files:
        step,file_id = recorded.get(item.index,(None,None))
        if step!='done':
            raise ValueError(tr('文件日志完成状态无效'))
        item.check(item.target,file_id)


def run(db,files,keep,stats,on_progress,cancelled):
    states = {index:(step,(device,inode)) for index,step,device,inode in
              db.execute('SELECT id,step,device,inode FROM operations')}
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
        states = {i:(step,(device,inode)) for i,step,device,inode in
                  db.execute('SELECT id,step,device,inode FROM operations')}
    stats['files_moved'] = len(files)
    stats['files_renamed'] = sum(item.classified.name!=item.target.name for item in files)
    verify_ready(db,files)
    return True
