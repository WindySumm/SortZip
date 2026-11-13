"""Portable name-restoration support. Generated scripts need Windows, not Python."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from sortzip_core.constants import validate_win_folder_name


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def write_bundle(folder, mappings):
    """Create an exclusive kit directory intended to sit beside its payload files.

    mappings contains (current Path, original basename). Absolute input paths are
    used only while building hashes; none are included in the portable manifest.
    """
    records, sources, targets, parents = [], set(), set(), set()
    for source, target in mappings:
        source = Path(source)
        if (validate_win_folder_name(source.name) or not isinstance(target,str)
                or validate_win_folder_name(target) or source.is_symlink() or not source.is_file()):
            raise ValueError('还原名称或文件无效')
        if source.name.casefold() in sources or target.casefold() in targets:
            raise ValueError('同一还原组包含重复的来源或目标名称')
        sources.add(source.name.casefold())
        targets.add(target.casefold())
        parents.add(str(source.parent.resolve()).casefold())
        if len(parents)>1:
            raise ValueError('同一脚本只能还原同一目录的文件')
        before = source.stat()
        digest = _hash(source)
        after = source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('生成还原清单期间文件发生变化')
        records.append(dict(**{'from':source.name}, to=target, size=before.st_size, sha256=digest))
    if not records:
        raise ValueError('还原清单不能为空')
    job = hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    for index, record in enumerate(records):
        name = f'.sortzip-restore-{job[:16]}-{index}.tmp'
        if name.casefold() in sources|targets:
            raise ValueError('临时还原名称与文件冲突')
        record['temp'] = name
    script = Path(__file__).with_name('restore_names.ps1').read_text(encoding='utf-8')
    manifest = json.dumps(dict(schema=1,job=job,files=records),ensure_ascii=False,indent=2)
    launcher = ('@echo off\r\n'
        'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restore-Names.ps1"\r\n'
        'set "restoreExit=%errorlevel%"\r\n'
        'pause\r\nexit /b %restoreExit%\r\n')
    desired = {'Restore-Names.json':manifest.encode('utf-8'),
        'Restore-Names.ps1':script.encode('utf-8-sig'), 'Restore-Names.cmd':launcher.encode('ascii')}
    folder = Path(folder)
    if folder.exists() or folder.is_symlink():
        if folder.is_symlink() or not folder.is_dir() or any(
                not (folder/name).is_file() or (folder/name).is_symlink() or (folder/name).read_bytes()!=data
                for name,data in desired.items()):
            raise FileExistsError('还原脚本目录已占用或内容被修改')
    else:
        folder.parent.mkdir(parents=True,exist_ok=True)
        staging = folder.with_name('.sortzip-kit-'+uuid.uuid4().hex+'.tmp')
        staging.mkdir()
        for name,data in desired.items():
            with (staging/name).open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        staging.rename(folder)
    return folder
