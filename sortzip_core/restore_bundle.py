"""Portable name-restoration support. Generated scripts need Windows, not Python."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from sortzip_core.constants import validate_win_folder_name
from sortzip_core.i18n import tr


PAYLOAD_PREFIX = 'SortZip-Restore-Names'
PAYLOAD_NAMES = {f'{PAYLOAD_PREFIX}.{ext}'.casefold() for ext in ('cmd','ps1','json')}
PAYLOAD_NAMES.add(f'{PAYLOAD_PREFIX}-State.json'.casefold())
PAYLOAD_NAMES.add(f'{PAYLOAD_PREFIX}.lock'.casefold())
# Existing checkpoints can reuse the unmodified external script shipped in v1.1.0.
LEGACY_SCRIPT_SHA256 = 'f4c531dd2501c0ab9003e10f3ee045a7d903b4986d87fedca86beda776d93c4d'


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def write_bundle(folder, mappings, *, embedded=False):
    """Create an exclusive kit directory intended to sit beside its payload files.

    A mapping may include a precomputed digest from the verified file journal.
    """
    records, sources, targets, parents = [], set(), set(), set()
    for source, target, *checksums in mappings:
        source = Path(source)
        if (validate_win_folder_name(source.name) or not isinstance(target,str)
                or validate_win_folder_name(target) or source.is_symlink() or not source.is_file()):
            raise ValueError(tr('还原名称或文件无效'))
        if source.name.casefold() in sources or target.casefold() in targets:
            raise ValueError(tr('同一还原组包含重复的来源或目标名称'))
        sources.add(source.name.casefold())
        targets.add(target.casefold())
        parents.add(str(source.parent.resolve()).casefold())
        if len(parents)>1:
            raise ValueError(tr('同一脚本只能还原同一目录的文件'))
        before = source.stat()
        digest = checksums[0] if checksums else _hash(source)
        after = source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError(tr('生成还原清单期间文件发生变化'))
        records.append(dict(**{'from':source.name}, to=target, size=before.st_size, sha256=digest))
    if not records:
        raise ValueError(tr('还原清单不能为空'))
    job = hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    for index, record in enumerate(records):
        name = f'.sortzip-restore-{job[:16]}-{index}.tmp'
        if name.casefold() in sources|targets:
            raise ValueError(tr('临时还原名称与文件冲突'))
        record['temp'] = name
    script = Path(__file__).with_name('restore_names.ps1').read_text(encoding='utf-8')
    prefix = PAYLOAD_PREFIX if embedded else 'Restore-Names'
    if embedded:
        script = script.replace("'Restore-Names.json'",f"'{prefix}.json'")
        script = script.replace("'Restore-State.json'",f"'{prefix}-State.json'")
        script = script.replace("'Restore-Names.lock'",f"'{prefix}.lock'")
        script = script.replace("[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))",'$PSScriptRoot')
    manifest = json.dumps(dict(schema=1,job=job,files=records),ensure_ascii=False,indent=2)
    launcher = ('@echo off\r\n'
        f'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0{prefix}.ps1"\r\n'
        'set "restoreExit=%errorlevel%"\r\n'
        'pause\r\nexit /b %restoreExit%\r\n')
    desired = {f'{prefix}.json':manifest.encode('utf-8'),
        f'{prefix}.ps1':script.encode('utf-8-sig'), f'{prefix}.cmd':launcher.encode('ascii')}
    folder = Path(folder)
    if folder.exists() or folder.is_symlink():
        if folder.is_symlink() or not folder.is_dir() or any(
                not _matches_bundle_file(folder/name,data,embedded=embedded)
                for name,data in desired.items()):
            raise FileExistsError(tr('还原脚本目录已占用或内容被修改'))
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


def _matches_bundle_file(path, content, *, embedded):
    if not path.is_file() or path.is_symlink():
        return False
    existing = path.read_bytes()
    return existing==content or (not embedded and path.name=='Restore-Names.ps1'
                                 and hashlib.sha256(existing).hexdigest()==LEGACY_SCRIPT_SHA256)


def validate_payload(plan, entries, *, context=None):
    rows = {row['source_rel']:row for row in entries}
    for task in plan.tasks:
        if task.layer_index:
            break
        if task.settings.archive_format in ('gz','xz'):
            raise ValueError(tr('第 1 层的 {0} 只能保存一个文件，无法附带原名还原脚本。',task.settings.archive_format.upper()))
        targets = {}
        for original in task.unit.original_ids:
            if context:
                context.check()
            name = Path(original).name
            member = rows[original]['target_name']
            reserved = next((value for value in (member,name) if value.casefold() in PAYLOAD_NAMES), None)
            conflict = targets.get(name.casefold()) or reserved
            if conflict:
                raise ValueError(tr('归档组 {0}/{1} 的原名还原存在名称冲突：{2}；另一个名称：{3}。',
                                    task.unit.folder,task.archive_stem,original,conflict))
            targets[name.casefold()] = original
