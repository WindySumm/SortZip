"""Portable name-restoration support. Generated scripts need Windows, not Python."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from sortzip_core.constants import validate_win_folder_name
from sortzip_core.i18n import tr


PAYLOAD_PREFIX = 'SortZip-Restore-Names'
CHINESE_PAYLOAD_LAUNCHER = '还原原文件名.cmd'
CHINESE_CATEGORY_LAUNCHER = '还原压缩包后缀.cmd'
CATEGORY_RESTORE_LOCK = '.sortzip-restore-names.lock'
PAYLOAD_NAMES = {f'{PAYLOAD_PREFIX}.{ext}'.casefold() for ext in ('cmd','ps1','json')}
PAYLOAD_NAMES.add(CHINESE_PAYLOAD_LAUNCHER.casefold())
PAYLOAD_NAMES.add(f'{PAYLOAD_PREFIX}-State.json'.casefold())
PAYLOAD_NAMES.add(f'{PAYLOAD_PREFIX}.lock'.casefold())
# Existing checkpoints can reuse the unmodified external script shipped in v1.1.0.
LEGACY_SCRIPT_SHA256 = 'f4c531dd2501c0ab9003e10f3ee045a7d903b4986d87fedca86beda776d93c4d'
LEGACY_CATEGORY_SCRIPT_SHA256 = '21d020b09a0e2aa11e54c8eedf3bd8ffd2983dfab4e4068d282508e2835b4606'
PREVIOUS_CATEGORY_SCRIPT_SHA256 = '10e2ab53ffd5b68fdc5f7182e10e6e091404e3f416a5c233b975afa90e4fd1cb'
FOLDER_CATEGORY_SCRIPT_SHA256 = '12aa5b9c59c36d03ad05e6982ddab21e046a1e08c937c930cdc1e51aa895f5e2'
PREVIOUS_SINGLE_FILE_SCRIPT_SHA256 = '3696b4257ef165fc1236aa28811156479f851b14d9141958b5dca2a6ce5e1476'


def category_launcher_name(language):
    return CHINESE_CATEGORY_LAUNCHER if language=='zh' else 'Restore-Names.cmd'


def category_launcher(script, manifest, *, adjacent=False, report_outcome=True):
    """Keep the command line short and load the two UTF-8 payloads from the CMD itself."""
    placement = ' -Adjacent' if adjacent else ''
    diagnostic = '; Write-Host $_.Exception.Message' if report_outcome else ''
    command = (
        "$ErrorActionPreference='Stop'; try {"
        "$p=$env:SORTZIP_RESTORE_CMD; $lines=[IO.File]::ReadAllLines($p);"
        "$a=[Array]::IndexOf($lines,'::SORTZIP-SCRIPT');"
        "$b=[Array]::IndexOf($lines,'::SORTZIP-MANIFEST');"
        "if ($a -lt 0 -or $b -le ($a+2) -or $b -ge ($lines.Length-2)) { throw 'Invalid payload' };"
        "$sha=[Security.Cryptography.SHA256]::Create();"
        "function Decode-Part($start,$end) {"
        "$bytes=[Convert]::FromBase64String(($lines[($start+2)..$end] -join ''));"
        "$digest=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','');"
        "if ($digest -ine $lines[$start+1]) { throw 'Invalid payload checksum' }; ,$bytes };"
        "$script=Decode-Part $a ($b-1); $manifest=Decode-Part $b ($lines.Length-1); $sha.Dispose();"
        "$body=[Text.UTF8Encoding]::new($false,$true).GetString($script);"
        f"& ([ScriptBlock]::Create($body)) -KitFolder ([IO.Path]::GetDirectoryName($p)) -ManifestBytes $manifest{placement};"
        "exit 0"
        "} catch {"
        "if ([Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'zh') {"
        "Write-Host ([string][char]0x8FD8+[char]0x539F+[char]0x6587+[char]0x4EF6+[char]0x635F+[char]0x574F)"
        f"}} else {{ Write-Host 'The restore file is damaged.' }}{diagnostic}; exit 1 }}"
    )
    lines = ['@echo off','setlocal DisableDelayedExpansion','set "SORTZIP_RESTORE_CMD=%~f0"',
             f'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "{command}"',
             'set "restoreExit=%errorlevel%"']
    if report_outcome:
        lines.extend(('if not "%restoreExit%"=="0" if not "%restoreExit%"=="2" set "restoreExit=1"',
                      'if not "%restoreExit%"=="0" pause'))
    else:
        lines.append('pause')
    lines.append('exit /b %restoreExit%')
    for label,payload in (('SCRIPT',script),('MANIFEST',manifest)):
        lines.extend((f'::SORTZIP-{label}',hashlib.sha256(payload).hexdigest()))
        encoded = base64.b64encode(payload).decode('ascii')
        lines.extend(encoded[index:index+76] for index in range(0,len(encoded),76))
    return ('\r\n'.join(lines)+'\r\n').encode('ascii')


def _matches_previous_launcher(existing,manifest,*,adjacent=False):
    lines = existing.splitlines()
    try:
        start = lines.index(b'::SORTZIP-SCRIPT')+2
        end = lines.index(b'::SORTZIP-MANIFEST',start)
        script = base64.b64decode(b''.join(lines[start:end]),validate=True)
    except ValueError:
        return False
    allowed = (PREVIOUS_SINGLE_FILE_SCRIPT_SHA256,) if adjacent else (
        PREVIOUS_SINGLE_FILE_SCRIPT_SHA256,FOLDER_CATEGORY_SCRIPT_SHA256)
    return (hashlib.sha256(script).hexdigest() in allowed
            and existing==category_launcher(script,manifest,adjacent=adjacent,report_outcome=False))


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def write_bundle(folder, mappings, *, embedded=False, language=None):
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
    launcher_name = CHINESE_PAYLOAD_LAUNCHER if embedded and language=='zh' else f'{prefix}.cmd'
    if embedded:
        script = script.replace("'Restore-Names.json'",f"'{prefix}.json'")
        script = script.replace("'Restore-State.json'",f"'{prefix}-State.json'")
        script = script.replace("'Restore-Names.lock'",f"'{prefix}.lock'")
        script = script.replace("[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))",'$PSScriptRoot')
        if language is not None:
            script = script.replace("([IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Name)+'.cmd')",
                                    f"'{launcher_name}'")
            script = localized_embedded_script(script)
    manifest = json.dumps(dict(schema=1,job=job,files=records),ensure_ascii=False,indent=2)
    launcher = ('@echo off\r\n'
        f'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0{prefix}.ps1"\r\n'
        'set "restoreExit=%errorlevel%"\r\n'
        'pause\r\nexit /b %restoreExit%\r\n')
    desired = {f'{prefix}.json':manifest.encode('utf-8'),
        f'{prefix}.ps1':script.encode('utf-8-sig'), launcher_name:launcher.encode('ascii')}
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


def localized_embedded_script(script):
    flag = "$zh = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'zh'\n"
    script = script.replace("$ErrorActionPreference = 'Stop'", "$ErrorActionPreference = 'Stop'\n"+flag)
    for english,chinese in (
            ('Already restored. No files changed.','文件名已还原。'),
            ('Names restored. No extraction was performed.','原文件名已还原。'),
            ('Keep this folder and all temporary files. Resolve the reported issue and run again.',
             '请保留此文件夹和临时文件。处理提示的问题后再运行。')):
        script = script.replace(f"Write-Host '{english}'",
                                f"if ($zh) {{ Write-Host '{chinese}' }} else {{ Write-Host '{english}' }}")
    for english,chinese in (
            ('Unsafe filename in manifest.','还原清单含有无效的文件名。'),
            ('Not a regular file: ','不是普通文件：'),
            ('Invalid manifest.','还原清单无效。'),
            ('Duplicate filename mapping.','还原清单有重复的文件名。'),
            ('Invalid file identity.','还原清单中的文件校验信息无效。'),
            ('Temporary name overlaps payload.','暂存名称与待还原文件冲突。'),
            ('Mapping overlaps the restore files: ','还原目标与脚本文件冲突：'),
            ('State does not match this restore job.','恢复记录与这次还原不匹配。'),
            ('Temporary name already exists: ','暂存文件已存在：'),
            ('Input missing or changed: ','文件缺失或已变化：'),
            ('Target already exists: ','目标文件已存在：'),
            ('Unchanged file missing or modified: ','文件缺失或已变化：'),
            ('Both the input and temporary name exist: ','原文件与暂存文件同时存在：'),
            ('Staged file changed: ','暂存文件已变化：'),
            ('Restored file changed or missing: ','已还原文件缺失或已变化：'),
            ('Restored file missing or changed: ','已还原文件缺失或已变化：'),
            ('Could not lock this restore job. Close any other restore window and try again: ',
             '无法锁定还原任务。请关闭其他还原窗口后重试：')):
        pattern = r"""throw (['"])(%s[^'"\r\n]*)\1""" % re.escape(english)
        script = re.sub(pattern,lambda match:
            f"if ($zh) {{ throw {match[1]}{chinese}{match[2][len(english):]}{match[1]} }} "
            f"else {{ {match[0]} }}",script)
    return script.replace('Write-Host "Restore stopped: $($_.Exception.Message)"',
                          'if ($zh) { Write-Host "还原已停止。详情：$($_.Exception.Message)" } '
                          'else { Write-Host "Restore stopped: $($_.Exception.Message)" }')


def write_category_bundle(folder,mappings,*,language=None,adjacent=False):
    """Publish one partial, restartable restore kit beside a category's archives."""
    from sortzip_core import file_journal
    from tempfile import TemporaryDirectory

    category = folder if adjacent else folder.parent
    launcher_name = category_launcher_name(language)
    records = []
    reserved = {launcher_name.casefold(),CATEGORY_RESTORE_LOCK} if adjacent else set()
    for source,target,digest in mappings:
        relative = source.relative_to(category)
        destination = relative.with_name(target)
        pair = set()
        for path in (relative,destination):
            candidate = file_journal.safe_path(category,path.as_posix())
            key = path.as_posix().casefold()
            if key in reserved or candidate.is_junction():
                raise ValueError(tr('同一还原组包含重复的来源或目标名称'))
            pair.add(key)
        reserved.update(pair)
        before = file_journal.identity(source)
        digest = digest or _hash(source)
        if file_journal.identity(source)!=before:
            raise ValueError(tr('生成还原清单期间文件发生变化'))
        records.append({'from':relative.as_posix(),'to':destination.as_posix(),
                        'size':before[2],'sha256':digest})
    manifest = json.dumps(dict(schema=2,files=records),ensure_ascii=False,indent=2).encode('utf-8')
    script = Path(__file__).with_name('restore_category.ps1').read_text(encoding='utf-8')
    launcher = ('@echo off\r\n'
                'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restore-Names.ps1"\r\n'
                'set "restoreExit=%errorlevel%"\r\npause\r\nexit /b %restoreExit%\r\n')
    desired = category_launcher(script.encode('utf-8'),manifest,adjacent=adjacent)
    if adjacent:
        target = file_journal.safe_path(category,launcher_name)
        lock = file_journal.safe_path(category,CATEGORY_RESTORE_LOCK)
        if file_journal.exists(lock):
            raise FileExistsError(tr('输出位置已被占用：{0}',lock))
        if file_journal.exists(target):
            if target.is_file():
                file_journal.identity(target)
                existing = target.read_bytes()
                if existing==desired or _matches_previous_launcher(existing,manifest,adjacent=True):
                    return
            raise FileExistsError(tr('输出位置已被占用：{0}',target))
        with TemporaryDirectory(prefix='.sortzip-kit-',dir=category) as temporary:
            staged = Path(temporary)/launcher_name
            with staged.open('xb') as stream:
                stream.write(desired)
                stream.flush()
                os.fsync(stream.fileno())
            file_journal.rename_new(staged,target)
        return
    if file_journal.exists(folder):
        if not folder.is_dir() or folder.is_junction() or folder.is_symlink():
            raise FileExistsError(tr('输出位置已被占用：{0}',folder))
        def regular_bytes(name):
            path = folder/name
            return path.read_bytes() if path.is_file() and not path.is_symlink() else b''
        existing = regular_bytes(launcher_name)
        if existing==desired or _matches_previous_launcher(existing,manifest):
            return
        if existing==launcher.encode('ascii') and regular_bytes('Restore-Names.json')==manifest:
            digest = hashlib.sha256(regular_bytes('Restore-Names.ps1')).hexdigest()
            if digest==PREVIOUS_CATEGORY_SCRIPT_SHA256 or (language is None and digest==LEGACY_CATEGORY_SCRIPT_SHA256):
                return
        raise FileExistsError(tr('输出位置已被占用：{0}',folder))
    with TemporaryDirectory(prefix='.sortzip-kit-',dir=category) as temporary:
        staging = Path(temporary)/folder.name
        staging.mkdir()
        with (staging/launcher_name).open('xb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        staging.rename(folder)


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
