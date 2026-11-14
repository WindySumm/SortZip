"""Command-line archivers used by the multi-layer workspace."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from sortzip_core.formats import FORMATS, RAR_FORMAT
from sortzip_core.planning import StepError
from sortzip_core.i18n import tr


TOOL_NAMES = {'bandizip': 'Bandizip', '7zip': '7-Zip', 'winrar': 'WinRAR'}
TOOL_EXECUTABLES = {'bandizip': ('bz.exe',), '7zip': ('7z.exe','7za.exe'), 'winrar': ('Rar.exe',)}
TOOL_DIRECTORIES = {'bandizip':'Bandizip','7zip':'7-Zip','winrar':'WinRAR'}
TOOL_FORMATS = {'bandizip':FORMATS,'7zip':{fmt:FORMATS[fmt] for fmt in ('zip','7z')},'winrar':{'rar':RAR_FORMAT}}


def detect_tool(tool, preferred=''):
    """Resolve a locally configured executable. Never resolve a path from a checkpoint."""
    if tool not in TOOL_NAMES:
        raise ValueError(tr('未知的压缩工具'))
    if preferred:
        candidate = Path(preferred)
        if tool=='bandizip':
            # Older preferences may point to the graphical launcher. Use its
            # sibling console executable when present, without running the GUI.
            if candidate.name.lower()=='bandizip.exe':
                candidate = candidate.with_name('bz.exe')
            if candidate.name.lower()!='bz.exe':
                return None
        if candidate.is_absolute() and candidate.suffix.lower()=='.exe' and candidate.is_file():
            resolved = candidate.resolve()
            if tool!='bandizip' or resolved.name.lower()=='bz.exe':
                return str(resolved)
        return None
    for name in TOOL_EXECUTABLES[tool]:
        for directory in os.get_exec_path():
            if Path(directory).is_absolute():
                candidate = Path(directory)/name
                detected = detect_tool(tool,str(candidate))
                if detected:
                    return detected
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        directory = os.environ.get(env)
        if directory:
            folder = TOOL_DIRECTORIES[tool]
            for name in TOOL_EXECUTABLES[tool]:
                candidate = Path(directory)/folder/name
                if candidate.is_absolute():
                    detected = detect_tool(tool,str(candidate))
                    if detected:
                        return detected
    return None


def resolve_tool(tool, paths=None, legacy_bandizip=None):
    preferred = (paths or {}).get(tool, '')
    if tool=='bandizip' and not preferred and legacy_bandizip:
        # Older programmatic callers may supply an executable basename.
        preferred = str(legacy_bandizip)
        if preferred.lower() in ('bz','bz.exe','bandizip','bandizip.exe'):
            preferred = ''
        elif not Path(preferred).is_absolute():
            preferred = shutil.which(preferred) or preferred
    executable = detect_tool(tool, preferred)
    if not executable:
        if tool=='bandizip':
            raise ValueError(tr('找不到 Bandizip 命令行程序 bz.exe。请在设置中选择 Bandizip 安装目录下的 bz.exe。'))
        raise ValueError(tr('找不到 {0} 命令行程序。请在设置中选择有效的程序路径。', TOOL_NAMES[tool]))
    return executable


def check_tool(tool, paths=None, legacy_bandizip=None):
    executable = resolve_tool(tool, paths, legacy_bandizip)
    probe = {'bandizip':(), '7zip':('i',), 'winrar':('-?',)}[tool]
    try:
        result = subprocess.run([executable, *probe], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=8, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(tr('{0} 无法启动或未在 8 秒内响应：{1}', TOOL_NAMES[tool], executable)) from error
    banner = (result.stdout+result.stderr).lower()
    if ((tool=='bandizip' and (b'bandizip' not in banner or b'<command>' not in banner)) or
            (tool=='7zip' and (result.returncode!=0 or b'7-zip' not in banner)) or
            (tool=='winrar' and not re.search(rb'(?m)^rar [5-9]\.\d+',banner))):
        raise ValueError(tr('所选程序未通过 {0} 命令行检测：{1}', TOOL_NAMES[tool], executable))
    return executable


def executable_identity(executable):
    path = Path(executable)
    before = path.stat()
    with path.open('rb') as stream:
        import hashlib
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
    after = path.stat()
    def identity(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    if identity(before)!=identity(after) or path.resolve()!=path:
        raise ValueError(tr('压缩程序在检测期间发生变化：{0}', path))
    return (*identity(after),digest)


def validate_tool_layers(tool, layers):
    if tool not in TOOL_FORMATS:
        raise ValueError(tr('未知的压缩工具'))
    for index, layer in enumerate(layers):
        caps = TOOL_FORMATS[tool].get(layer.archive_format)
        prefix = tr('第 {0} 层：{1} ', index+1, TOOL_NAMES[tool])
        if caps is None:
            raise StepError(f'layer:{index}', prefix+tr('不能创建 {0}，请更改格式或压缩工具', layer.archive_format.upper()))
        if layer.enable_volume and not caps['volume']:
            raise StepError(f'layer:{index}', prefix+tr('未开放 {0} 分卷，请关闭分卷或更改格式', layer.archive_format.upper()))
        if layer.password and not caps['password']:
            raise StepError(f'layer:{index}', prefix+tr('不支持当前格式的密码，请调整格式或密码'))
        if tool!='bandizip' and '"' in layer.password:
            raise StepError(f'layer:{index}', prefix+tr('的命令行无法可靠传递双引号密码，请更换密码'))
        if tool=='7zip' and layer.archive_format=='zip' and any(not 32<=ord(char)<=126 for char in layer.password):
            raise StepError(f'layer:{index}', prefix+tr('创建 ZIP 时只接受可打印 ASCII 密码，请使用英文字母、数字或符号'))


@dataclass(frozen=True)
class ArchiveTool:
    identifier: str
    executable: str

    @property
    def name(self):
        return TOOL_NAMES[self.identifier]

    @contextmanager
    def create_command(self, archive, paths, settings, volume):
        if self.identifier=='bandizip':
            command = [self.executable, 'a', '-fmt:'+settings.archive_format, '-y']
            if settings.password:
                command.append('-p:'+settings.password)
            if volume:
                command.append('-v:'+volume)
            yield [*command, str(archive), *paths]
            return

        if self.identifier=='winrar':
            command = [self.executable,'a','-cfg-','-ma5','-ep','-scflr','-y']
        else:
            command = [self.executable,'a','-t'+settings.archive_format,'-y','-spd','-scsUTF-8','-sccUTF-8']
        if settings.password:
            command.append('-p'+settings.password)
            if settings.archive_format=='zip':
                command.append('-mem=AES256')
        if volume:
            size = volume.lower()
            command.append('-v'+(size[:-1] if size.endswith('b') else size))
        with tempfile.TemporaryDirectory(prefix='.sortzip-inputs-',dir=archive.parent) as temporary:
            listing = Path(temporary)/'inputs.txt'
            listing.write_text('\n'.join(paths)+'\n', encoding='utf-8')
            yield [*command, str(archive), '@'+str(listing)]

    def test_command(self, archive, password):
        command = [self.executable, 't', '-y']
        if self.identifier=='7zip':
            command.extend(('-spd', '-sccUTF-8'))
        elif self.identifier=='winrar':
            command.extend(('-cfg-','-scfr'))
        if password:
            command.append(('-p:' if self.identifier=='bandizip' else '-p')+password)
        return [*command, str(archive)]

    def list_command(self, archive, password):
        if self.identifier=='bandizip':
            return [self.executable,'l','-list:s','-consolemode:utf8',
                    *(['-p:'+password] if password else []),str(archive)], 'utf-8-sig'
        if self.identifier=='7zip':
            return [self.executable,'l','-slt','-ba','-sccUTF-8',
                    *(['-p'+password] if password else []),str(archive)], 'utf-8-sig'
        return [self.executable,'lt','-v','-cfg-','-scfr','-p'+(password or '-'),str(archive)], 'utf-8-sig'

    def extract_command(self, archive, folder, password, member=None):
        if self.identifier=='bandizip':
            command = [self.executable,'e','-y','-aos','-consolemode:utf8','-o:'+str(folder)]
            if password:
                command.append('-p:'+password)
        elif self.identifier=='7zip':
            command = [self.executable,'e','-y','-aos','-spd','-sccUTF-8','-o'+str(folder),'-p'+password]
        else:
            command = [self.executable,'e','-cfg-','-scfr','-y','-o-','-p'+(password or '-')]
        command.append(str(archive))
        if member:
            command.append(member)
        if self.identifier=='winrar':
            command.append(str(folder)+os.sep)
        return command

    def collect(self, archive, settings):
        from sortzip_core.engine import _first_artifacts
        files = list(archive.parent.iterdir())
        if any(not file.is_file() or file.is_symlink() for file in files):
            raise ValueError(tr('压缩产物目录含有无效文件'))
        if not settings.enable_volume:
            expected = [archive]
        elif self.identifier=='bandizip':
            expected = _first_artifacts(archive.parent, archive.stem, '', archive_format=settings.archive_format)
        elif archive.is_file():
            expected = [archive]
        else:
            expected = files
        if set(files)!=set(expected):
            raise ValueError(tr('压缩产物缺失、存在额外文件或分卷不连续'))
        entry = entry_name(self.identifier,archive.stem,settings,{file.name for file in expected})
        return sorted(expected),archive.parent/entry


def entry_name(tool,stem,settings,names):
    """Validate an exact set of canonical filenames and return its entry volume."""
    fmt = settings.archive_format
    base = stem+'.'+fmt
    if names=={base}:
        return base
    if not settings.enable_volume:
        raise ValueError(tr('分卷未启用或缺少归档入口'))
    if tool=='winrar':
        pattern = r'\.part(\d+)\.rar'
    elif tool=='bandizip' and fmt=='zip' and base in names:
        pattern = r'\.z(\d{2,})'
    else:
        pattern = re.escape('.'+fmt)+r'\.(\d{3,})'
    indexed = {}
    for name in names:
        if name==base and tool=='bandizip' and fmt=='zip':
            continue
        match = re.fullmatch(re.escape(stem)+pattern,name)
        if match is None or int(match[1]) in indexed:
            raise ValueError(tr('存在无效或重复分卷'))
        indexed[int(match[1])] = name
    if not indexed or sorted(indexed)!=list(range(1,len(indexed)+1)):
        raise ValueError(tr('缺少入口卷或分卷不连续'))
    return base if base in names else indexed[1]


def shown_name(canonical, stem, archive_format, suffix):
    if suffix is None:
        return canonical
    tail = canonical[len(stem):]
    extension = '.'+archive_format
    if tail.startswith(extension):
        tail = tail[len(extension):]
    elif archive_format=='rar' and tail.endswith(extension):
        return stem+tail[:-len(extension)]+suffix
    return stem+suffix+tail
