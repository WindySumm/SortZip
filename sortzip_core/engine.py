import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath

from sortzip_core.constants import RESUME_FILE_NAME, validate_win_folder_name
from sortzip_core import __version__
from sortzip_core.config import prepare_config
from sortzip_core.formats import FORMATS, validate_compression, validate_volume, final_suffix
# Compatibility exports for existing scripts using engine's original helper names.
from sortzip_core.planning import (SORT_FUNCS, _natural_key, _sort_files, build_execution_plan,
                                  render_template, _match_rule, _match_rule_hierarchy, check_naming_conflicts)
from sortzip_core.i18n import tr

SEP = "\t"


class ForceCancelled(Exception):
    pass


def redact(text, password):
    return str(text).replace(password, '********') if password else str(text)


def _command_text(cmd):
    return ' '.join(tr('密码:********') if arg.startswith('-p') else arg for arg in cmd)


def _run_proc(cmd, force_cancel_check=None, wait_ms=200):
    """Run a command unattended, polling every `wait_ms` ms for force-cancel."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')
    while True:
        if force_cancel_check and force_cancel_check():
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
            raise ForceCancelled()
        try:
            stdout, stderr = proc.communicate(timeout=wait_ms / 1000.0)
            break
        except subprocess.TimeoutExpired:
            continue
    if force_cancel_check and force_cancel_check():
        raise ForceCancelled()
    password = next((arg[2:] for arg in cmd[2:] if arg.startswith('-p')), '')
    stdout, stderr = redact(stdout,password), redact(stderr,password)
    if password.startswith(':'):
        # Bandizip uses -p:VALUE. Other tools can pass a password starting with a colon.
        stdout, stderr = redact(stdout,password[1:]), redact(stderr,password[1:])
    if proc.returncode != 0:
        safe_cmd = ['-p:********' if arg.startswith('-p') else arg for arg in cmd]
        raise subprocess.CalledProcessError(proc.returncode, safe_cmd, stdout, stderr)
    return stdout, stderr


def _check_cancel(cancel_check):
    if cancel_check and cancel_check():
        print(tr('用户取消了操作'))
        return True
    return False


def _cancel_status(force_cancel_check):
    if force_cancel_check and force_cancel_check():
        return "force_cancelled"
    return "safe_cancelled"


def _dedup_name(dest_dir, stem, ext):
    name = f"{stem}{ext}"
    counter = 0
    while (dest_dir / name).exists():
        counter += 1
        name = f"{stem}_{counter}{ext}"
    return name


def classify_files(src_dir, dest_root, custom_names=None, on_progress=None, cancel_check=None,
                   keep_files=False, recursive=False, keep_hierarchy=False, execution_plan=None):
    src_path = Path(src_dir)
    dest_root = Path(dest_root)
    if execution_plan is not None:
        created_dirs = set()
        for idx, entry in enumerate(execution_plan, 1):
            if _check_cancel(cancel_check):
                break
            current = entry['source'].stat()
            if current.st_size != entry['size'] or current.st_mtime_ns != entry['mtime_ns']:
                raise ValueError(tr('分类前源文件发生变化：{0}', entry['source_rel']))
            target_dir = dest_root / entry['folder']
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / entry['classified_name']
            if target.exists() or target.is_symlink():
                raise FileExistsError(tr('分类目标已存在：{0}', target))
            (shutil.copy2 if keep_files else shutil.move)(str(entry['source']), str(target))
            print(f"{tr('复制') if keep_files else tr('移动')}: {entry['source_rel']} -> {target}")
            created_dirs.add(target_dir)
            if on_progress:
                on_progress(idx, len(execution_plan), tr('分类: {0}', entry['source_rel']))
        return created_dirs
    dest_root.mkdir(parents=True, exist_ok=True)
    if recursive:
        files = [f for f in src_path.rglob('*') if f.is_file() and f.name.casefold() != RESUME_FILE_NAME]
    else:
        files = [f for f in src_path.iterdir() if f.is_file() and f.name.casefold() != RESUME_FILE_NAME]
    total = len(files)
    created_dirs = set()
    for idx, file_path in enumerate(files, start=1):
        if _check_cancel(cancel_check):
            return created_dirs
        ext = file_path.suffix.lower()
        if custom_names and ext in custom_names:
            folder_name = custom_names[ext]
        elif custom_names is not None:
            continue
        else:
            folder_name = ext[1:] if ext else 'no_extension'
        if keep_hierarchy:
            rel = file_path.relative_to(src_path)
            target_dir = dest_root / folder_name / rel.parent
        else:
            target_dir = dest_root / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        created_dirs.add(target_dir)
        dest_name = _dedup_name(target_dir, file_path.stem, file_path.suffix)
        dest_path = target_dir / dest_name
        if keep_files:
            shutil.copy2(str(file_path), str(dest_path))
            print(tr('复制: {0} -> {1}', file_path.name, dest_path))
        else:
            shutil.move(str(file_path), str(dest_path))
            print(tr('移动: {0} -> {1}', file_path.name, dest_path))
        if on_progress:
            on_progress(idx, total, tr('分类: {0}', file_path.name))
    return created_dirs


def _disp_len(text):
    width = 0
    for c in text:
        if unicodedata.east_asian_width(c) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width


def _pad_center(text, width):
    dlen = _disp_len(text)
    pad = width - dlen
    if pad <= 0:
        return text
    left = (pad + 1) // 2
    right = pad - left
    return ' ' * left + text + ' ' * right


def _wrap_text(text, width):
    chunks = []
    remaining = text
    while remaining:
        for take in range(len(remaining), -1, -1):
            if take == 0:
                chunks.append('')
                remaining = ''
                break
            if _disp_len(remaining[:take]) <= width:
                chunks.append(remaining[:take])
                remaining = remaining[take:]
                break
    return chunks


def _collect_dirs(root, keep_hierarchy=False, only_dirs=None):
    root = Path(root)
    if only_dirs is not None:
        resolved_root = root.resolve()
        return sorted((Path(d) for d in only_dirs if Path(d).is_dir()
                       and Path(d).resolve().is_relative_to(resolved_root)
                       and (keep_hierarchy or Path(d).parent == root)), key=lambda d: d.relative_to(root))
    if keep_hierarchy:
        dirs = []
        for f in root.rglob('*'):
            if f.is_dir() and any(x.is_file() for x in f.iterdir()):
                if only_dirs is not None and f not in only_dirs:
                    continue
                dirs.append(f)
        return sorted(dirs, key=lambda d: d.relative_to(root))
    else:
        dirs = sorted(f for f in root.iterdir() if f.is_dir())
        if only_dirs is not None:
            dirs = [d for d in dirs if d in only_dirs]
        return dirs


def write_rename_list(dest_root, naming_rules, sort_by='name', group_size=1, archive_suffix='.zip',
                      compression_enabled=True, keep_hierarchy=False, only_dirs=None, execution_plan=None):
    if execution_plan is not None:
        import csv
        by_folder = {}
        for entry in execution_plan:
            by_folder.setdefault(entry['folder'], []).append(entry)
        folders = sorted(by_folder)
        for folder in folders:
            path = Path(dest_root) / folder / 'List.txt'
            with path.open('x', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream, delimiter='\t')
                writer.writerow(['序号', '原文件名', '新文件名'] + (['所属压缩包名'] if compression_enabled else []))
                for entry in by_folder[folder]:
                    writer.writerow([entry['index'], entry['source_rel'], entry['target_name']] +
                                    ([entry['archive']] if compression_enabled else []))
        return
    if compression_enabled:
        COL_W = (4, 24, 24, 24)
        HEADERS = ("序号", "原文件名", "新文件名", "所属压缩包名")
    else:
        COL_W = (4, 24, 24)
        HEADERS = ("序号", "原文件名", "新文件名")
    dest_root = Path(dest_root)
    for folder in _collect_dirs(dest_root, keep_hierarchy, only_dirs):
        files = [f for f in folder.iterdir() if f.is_file() and f.name.casefold() != RESUME_FILE_NAME]
        if not files:
            continue
        if keep_hierarchy:
            rule = _match_rule_hierarchy(naming_rules, folder, dest_root) if naming_rules else None
        else:
            rule = _match_rule(naming_rules, folder.name) if naming_rules else None
        template = rule.get('template', '') if rule else ''
        _sort_files(files, sort_by)

        total = len(files)
        hdr_cells = [_pad_center(h, COL_W[i]) for i, h in enumerate(HEADERS)]
        lines = [SEP.join(hdr_cells)]

        for idx, f in enumerate(files, start=1):
            new_name = render_template(template, idx, f.suffix, folder.name, f.stem) or f.name

            idx_str = str(idx)
            orig_str = f.name
            new_str = new_name

            idx_lines = _wrap_text(idx_str, COL_W[0])
            orig_lines = _wrap_text(orig_str, COL_W[1])
            new_lines = _wrap_text(new_str, COL_W[2])
            if compression_enabled:
                g = (idx - 1) // group_size
                s = g * group_size + 1
                e = min(g * group_size + group_size, total)
                base = str(s) if s == e else f"{s}-{e}"
                archive_name = f"{base}{archive_suffix}"
                arch_str = archive_name
                arch_lines = _wrap_text(arch_str, COL_W[3])
                max_rows = max(len(idx_lines), len(orig_lines), len(new_lines), len(arch_lines))
            else:
                max_rows = max(len(idx_lines), len(orig_lines), len(new_lines))

            for ri in range(max_rows):
                a = _pad_center(idx_lines[ri] if ri < len(idx_lines) else '', COL_W[0])
                b = _pad_center(orig_lines[ri] if ri < len(orig_lines) else '', COL_W[1])
                c = _pad_center(new_lines[ri] if ri < len(new_lines) else '', COL_W[2])
                if compression_enabled:
                    d = _pad_center(arch_lines[ri] if ri < len(arch_lines) else '', COL_W[3])
                    lines.append(SEP.join((a, b, c, d)))
                else:
                    lines.append(SEP.join((a, b, c)))

        list_path = folder / "List.txt"
        list_path.write_text("\n".join(lines), encoding="utf-8")
        print(tr('已输出命名对照表: {0}', list_path))


def rename_files_in_folders(dest_root, sort_by='name', on_progress=None, cancel_check=None,
                            naming_rules=None, keep_hierarchy=False, preview_order=None, only_dirs=None,
                            execution_plan=None, stats=None):
    dest_root = Path(dest_root)
    folders = _collect_dirs(dest_root, keep_hierarchy, only_dirs)
    done = 0
    total = 0
    for folder in folders:
        total += len([f for f in folder.iterdir() if f.is_file() and f.name.casefold() not in ('list.txt', RESUME_FILE_NAME)])
    if execution_plan is not None:
        total = len(execution_plan)
    folder_order = {}
    by_folder = {}
    if execution_plan is not None:
        for entry in execution_plan:
            by_folder.setdefault(dest_root / entry['folder'], []).append(entry)
    for folder in folders:
        if _check_cancel(cancel_check):
            return None
        files = [f for f in folder.iterdir() if f.is_file() and f.name.casefold() not in ('list.txt', RESUME_FILE_NAME)]
        planned = None
        if execution_plan is not None:
            planned = by_folder.get(folder, [])
            files = [folder / e['classified_name'] for e in planned]
        if not files:
            continue
        if planned is not None:
            pass
        elif preview_order and folder.name in preview_order:
            ordered = preview_order[folder.name]
            name_map = {f.name: f for f in files}
            files = [name_map[n] for n in ordered if n in name_map] + [f for f in files if f.name not in ordered]
        else:
            _sort_files(files, sort_by)
        if keep_hierarchy:
            rule = _match_rule_hierarchy(naming_rules, folder, dest_root) if naming_rules else None
        else:
            rule = _match_rule(naming_rules, folder.name) if naming_rules else None
        if not rule and planned is None:
            print(tr('跳过重命名（未匹配规则）: {0}', folder.name))
            done += len(files)
            folder_order[folder] = files[:]
            continue
        template = rule.get('template', '') if rule else ''
        targets = ([folder / e['target_name'] for e in planned] if planned is not None else
                   [folder / (render_template(template, idx, f.suffix, folder.name, f.stem) or f.name)
                    for idx, f in enumerate(files, 1)])
        keys = [p.name.casefold() for p in targets]
        if len(set(keys)) != len(keys):
            raise ValueError(tr('重命名目标重复: {0}', folder))
        sources = {p.name.casefold() for p in files}
        for target in targets:
            if (validate_win_folder_name(target.name) or target.parent != folder
                    or target.name.casefold() == RESUME_FILE_NAME
                    or (planned is None and target.name == 'List.txt')):
                raise ValueError(tr('无效重命名目标: {0}', target))
            if target.exists() and target.name.casefold() not in sources:
                raise ValueError(tr('重命名目标已存在: {0}', target))
        # Finish each directory as a transaction; cancellation is checked at its boundary.
        staged = []
        try:
            for source, target in zip(files, targets):
                if source.name == target.name:
                    continue
                temp = folder / f".sortzip_rename_{uuid.uuid4().hex}.tmp"
                source.rename(temp)
                staged.append([source, temp, target])
            for entry in staged:
                source, temp, target = entry
                if target.exists():
                    raise FileExistsError(tr('重命名目标已存在: {0}', target))
                temp.rename(target)
                entry[1] = target
        except OSError as error:
            # Re-stage committed destinations before restoring originals (cycles are possible).
            for entry in staged:
                try:
                    temp = folder / f".sortzip_rename_{uuid.uuid4().hex}.tmp"
                    entry[1].rename(temp)
                    entry[1] = temp
                except OSError:
                    pass
            for source, current, target in staged:
                try:
                    if not source.exists():
                        current.rename(source)
                except OSError:
                    pass
            remaining = '; '.join(str(current) for _, current, _ in staged if current.exists())
            raise OSError(tr('重命名失败，文件已尽量回滚；保留位置: {0}', remaining or folder)) from error
        done += len(files)
        if stats is not None:
            stats['files_renamed'] += sum(source.name != target.name for source, target in zip(files, targets))
        folder_order[folder] = targets
        if on_progress:
            on_progress(done, total, tr('重命名: {0}', folder.name))
        print(tr('重命名完成: {0} ({1} 个文件)', folder, len(files)))
    return folder_order


def get_auto_volume(total_size_bytes):
    four_gb = 4 * 1024 * 1024 * 1024
    if total_size_bytes < four_gb:
        volume_bytes = total_size_bytes // 2
    else:
        volume_bytes = four_gb
    if volume_bytes < 1024 * 1024:
        return f"{max(1, volume_bytes // 1024)}k"
    elif volume_bytes >= 1024 * 1024 * 1024:
        gb = (volume_bytes + (1024*1024*1024 - 1)) // (1024*1024*1024)
        return f"{gb}g"
    else:
        mb = (volume_bytes + (1024*1024 - 1)) // (1024*1024)
        return f"{mb}m"


# ---- 恢复任务 ----

def _checkpoint_path(dest_root):
    return Path(dest_root) / RESUME_FILE_NAME


def _write_checkpoint(dest_root, data):
    path = _checkpoint_path(dest_root)
    tmp = path.parent / f".sortzip_checkpoint_{uuid.uuid4().hex}.tmp"
    try:
        with tmp.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as error:
        raise OSError(tr('断点写入失败，已停止清理文件: {0}', path)) from error
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _load_checkpoint(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        _validate_checkpoint(data, path.parent)
        return data
    except (OSError, ValueError, TypeError) as e:
        raise ValueError(tr('断点文件无法读取或无效: {0}（{1}）', path, e)) from e


def _safe_relative(root, value, single=False):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError(tr('断点路径无效'))
    win = PureWindowsPath(value)
    if win.drive or win.root or '..' in win.parts or (single and len(win.parts) != 1):
        raise ValueError(tr('断点路径越界'))
    if any(validate_win_folder_name(part) for part in win.parts):
        raise ValueError(tr('断点路径名称无效'))
    candidate = root / value
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(tr('断点路径越界'))
    return candidate


def _validate_checkpoint(data, root):
    if not isinstance(data, dict) or not isinstance(data.get('config'), dict) or not isinstance(data.get('groups'), list):
        raise ValueError(tr('断点结构无效'))
    cfg = data['config']
    if not isinstance(cfg.get('archive_format', 'zip'), str) or cfg.get('archive_format', 'zip') not in FORMATS:
        raise ValueError(tr('断点压缩格式无效'))
    if type(cfg.get('group_size', 1)) is not int or cfg.get('group_size', 1) < 1:
        raise ValueError(tr('分组大小无效'))
    for key in ('password', 'double_compress', 'auto_close', 'enable_volume', 'verify', 'keep_hierarchy', 'keep_files'):
        if key in cfg and type(cfg[key]) is not bool:
            raise ValueError(tr('断点配置类型无效: {0}', key))
    validate_volume(cfg.get('volume'))
    for key, default in (('archive_suffix', '.zipp'), ('first_suffix', '-First'), ('first_suffix_replace', None)):
        value = cfg.get(key, default)
        if key == 'first_suffix_replace' and value is None:
            continue
        if not isinstance(value, str) or (key != 'first_suffix' and not value):
            raise ValueError(tr('归档后缀无效: {0}', key))
        if validate_win_folder_name('archive' + value) or any(c in value for c in '*?[]'):
            raise ValueError(tr('归档后缀无效: {0}', key))
    seen_groups, seen_files = set(), set()
    for group in data['groups']:
        if not isinstance(group, dict) or group.get('state') not in ('pending', 'first_done', 'final_done'):
            raise ValueError(tr('断点分组状态无效'))
        folder = _safe_relative(root, group.get('folder'))
        base = group.get('base_name')
        if not isinstance(base, str) or not re.fullmatch(r'[1-9]\d*(?:-[1-9]\d*)?', base):
            raise ValueError(tr('断点归档名称无效'))
        identity = (str(folder.resolve()).casefold(), base)
        if identity in seen_groups:
            raise ValueError(tr('断点包含重复分组'))
        seen_groups.add(identity)
        files, sizes = group.get('files'), group.get('sizes', {})
        mtimes = group.get('mtimes', {})
        if not isinstance(mtimes, dict):
            raise ValueError(tr('断点修改时间记录无效'))
        if not isinstance(files, list) or not files or not isinstance(sizes, dict):
            raise ValueError(tr('断点源文件列表无效'))
        for name in files:
            source = _safe_relative(folder, name, single=True)
            key = str(source.resolve()).casefold()
            if key in seen_files or name.casefold() == RESUME_FILE_NAME:
                raise ValueError(tr('断点源文件重复或为保留文件'))
            seen_files.add(key)
        if any(name not in files or type(value) is not int for name, value in mtimes.items()):
            raise ValueError(tr('断点修改时间记录无效'))
        if any(name not in files or type(size) is not int or size < 0 for name, size in sizes.items()):
            raise ValueError(tr('断点文件大小记录无效'))


def _delete_checkpoint(dest_root):
    path = _checkpoint_path(dest_root)
    try:
        if path.exists():
            path.unlink()
            print(tr('断点文件已清除: {0}', path))
    except OSError as e:
        print(tr('警告: 清除断点文件失败 {0}: {1}', path, e))


def _resume_config_snapshot(group_size, password, volume_size, bandizip_path,
                            double_compress, sort_by, archive_suffix,
                            first_suffix, enable_volume, first_suffix_replace,
                            verify, keep_hierarchy, archive_format='zip'):
    return {
        "archive_format": archive_format,
        "group_size": group_size,
        "password": bool(password),
        "volume": volume_size,
        "bandizip": bandizip_path,
        "double_compress": double_compress,
        "auto_close": True,
        "sort_by": sort_by,
        "archive_suffix": archive_suffix,
        "first_suffix": first_suffix,
        "enable_volume": enable_volume,
        "first_suffix_replace": first_suffix_replace,
        "verify": verify,
        "keep_hierarchy": keep_hierarchy,
    }


def _build_group_plan(dest_root, folders, all_groups):
    groups = []
    for folder, group, start_i in all_groups:
        rel = str(folder.relative_to(dest_root)).replace('\\', '/')
        base_name = str(start_i + 1)
        if len(group) > 1:
            base_name = f"{start_i + 1}-{start_i + len(group)}"
        metadata = {f.name: f.stat() for f in group}
        groups.append({
            "folder": rel,
            "base_name": base_name,
            "files": [f.name for f in group],
            "sizes": {name: info.st_size for name, info in metadata.items()},
            "mtimes": {name: info.st_mtime_ns for name, info in metadata.items()},
            "state": "pending",
        })
    return groups


def _find_group_files(dest_root, group):
    folder = Path(dest_root) / group["folder"]
    files = []
    for name in group.get("files", []):
        p = folder / name
        if p.exists():
            files.append(p)
    return files


def _source_matches(path, group):
    try:
        info = path.stat()
        return (path.is_file()
                and (path.name not in group.get('sizes', {}) or info.st_size == group['sizes'][path.name])
                and (path.name not in group.get('mtimes', {}) or info.st_mtime_ns == group['mtimes'][path.name]))
    except OSError:
        return False


def validate_resume_files(checkpoint_path):
    """校验断点引用的源文件是否仍存在且未被修改。

    返回问题描述列表；空列表表示一切正常。仅校验尚未压缩（pending）的分组：
    final_done 与 first_done 分组的源文件可能已按规则删除，属于正常状态。
    """
    data = _load_checkpoint(checkpoint_path)
    dest_root = Path(checkpoint_path).parent
    problems = []
    for g in data.get('groups', []):
        if g.get('state') != 'pending':
            continue
        folder = Path(dest_root) / g['folder']
        label = tr('组 {0} ({1})', g.get('base_name', '?'), g.get('folder', '?'))
        if not folder.is_dir():
            problems.append(tr('{0}: 文件夹不存在', label))
            continue
        sizes = g.get('sizes') or {}
        mtimes = g.get('mtimes') or {}
        for name in g.get('files', []):
            path = folder / name
            try:
                info = path.stat()
                if not path.is_file():
                    problems.append(tr('{0}: 文件缺失: {1}', label, name))
                elif name in sizes and info.st_size != sizes[name]:
                    problems.append(tr('{0}: 文件已被修改: {1}（大小不一致）', label, name))
                elif name in mtimes and info.st_mtime_ns != mtimes[name]:
                    problems.append(tr('{0}: 文件修改时间已变化: {1}', label, name))
            except OSError:
                problems.append(tr('{0}: 文件缺失或无法读取: {1}', label, name))
    return problems


def _compress_first(folder, base_name, files, password, bandizip_path, volume_size,
                    enable_volume, first_suffix, first_suffix_replace,
                    force_cancel_check, archive_format='zip'):
    first_name = f"{base_name}{first_suffix}"
    zip_path = folder / f"{first_name}.{archive_format}"
    auto_vol = None
    if enable_volume:
        if volume_size is None:
            total_bytes = sum(f.stat().st_size for f in files)
            auto_vol = get_auto_volume(total_bytes)
            print(tr('  组 {0} 总大小: {1} GB，自动分卷大小 = {2}', base_name, format(total_bytes / (1024**3), '.2f'), auto_vol))
        else:
            auto_vol = None
    cmd = [bandizip_path, 'a', '-fmt:' + archive_format]
    if password:
        cmd.extend(['-p:' + password])
    if enable_volume and volume_size:
        cmd.extend(['-v:' + volume_size])
    elif enable_volume and auto_vol:
        cmd.extend(['-v:' + auto_vol])
    cmd.append('-y')
    cmd.append(str(zip_path))
    cmd.extend([str(f) for f in files])
    print(tr('第一次压缩: {0}', _command_text(cmd)))
    _run_proc(cmd, force_cancel_check)
    print(tr('成功创建分卷: {0} (及其分卷)', zip_path))
    if first_suffix_replace and not enable_volume:
        if zip_path.exists():
            new_zip_path = folder / f"{first_name}{first_suffix_replace}"
            if new_zip_path.exists():
                new_zip_path.unlink()
            zip_path.rename(new_zip_path)
            zip_path = new_zip_path
            print(tr('一次压缩后缀替换: {0}', zip_path.name))
    if archive_format == '7z' and enable_volume and not zip_path.exists():
        zip_path = folder / f"{first_name}.7z.001"
    return zip_path


def _first_artifacts(folder, base_name, first_suffix, first_suffix_replace=None, archive_format='zip'):
    prefix = base_name + first_suffix
    names = {prefix + '.' + archive_format}
    if first_suffix_replace:
        names.add(prefix + first_suffix_replace)
    artifacts = {folder / name for name in names if (folder / name).is_file()}
    # Successful Bandizip volumes are contiguous. Pending leftovers (including
    # gaps) are indexed separately once per directory before processing starts.
    sequences = {'zip': [('.z', 2), ('.zip.', 3)], '7z': [('.7z.', 3)]}
    for separator, width in sequences.get(archive_format, []):
        index = 1
        while True:
            path = folder / f'{prefix}{separator}{index:0{width}d}'
            if not path.is_file():
                break
            artifacts.add(path)
            index += 1
    return sorted(artifacts)


def _index_artifacts(folders, first_suffix, first_suffix_replace, archive_format):
    extensions = [re.escape('.' + archive_format)]
    if first_suffix_replace:
        extensions.append(re.escape(first_suffix_replace))
    if archive_format == 'zip':
        extensions += [r'\.z\d+', r'\.zip\.\d+']
    elif archive_format == '7z':
        extensions += [r'\.7z\.\d+']
    pattern = re.compile(r'([1-9]\d*(?:-[1-9]\d*)?)' + re.escape(first_suffix)
                         + '(?:' + '|'.join(extensions) + ')', re.I)
    result = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            match = pattern.fullmatch(path.name)
            if match and path.is_file():
                result.setdefault((folder, match[1]), []).append(path)
    return result


def _verify_archive(path, password, bandizip_path, force_cancel_check):
    if not path.is_file():
        raise OSError(tr('压缩产物缺失: {0}', path))
    cmd = [bandizip_path, 't']
    if password:
        cmd.append('-p:' + password)
    cmd.append('-y')
    cmd.append(str(path))
    print(tr('校验: {0}', path.name))
    _run_proc(cmd, force_cancel_check)
    print(tr('校验通过: {0}', path.name))


def _compress_second(folder, base_name, first_suffix, password, bandizip_path,
                     archive_suffix, force_cancel_check,
                     first_suffix_replace=None, verify=False, archive_format='zip'):
    volume_files = _first_artifacts(folder, base_name, first_suffix, first_suffix_replace, archive_format)
    if not volume_files:
        raise OSError(tr('未找到组 {0} 的一次压缩产物，无法二次打包', base_name))
    temp_zip_path = folder / f"最终压缩{base_name}.{archive_format}"
    final_zip_path = folder / f"{base_name}{archive_suffix}"
    if final_zip_path in volume_files:
        raise ValueError(tr('一次与二次压缩产物名称冲突'))
    if temp_zip_path.exists():
        temp_zip_path.unlink()
    cmd = [bandizip_path, 'a', '-fmt:' + archive_format]
    if password:
        cmd.append('-p:' + password)
    cmd.append('-y')
    cmd.append(str(temp_zip_path))
    cmd.extend(str(f) for f in volume_files)
    print(tr('二次打包: {0}', _command_text(cmd)))
    _run_proc(cmd, force_cancel_check)
    if not temp_zip_path.is_file():
        raise OSError(tr('二次打包产物缺失: {0}', temp_zip_path))
    if verify:
        _verify_archive(temp_zip_path, password, bandizip_path, force_cancel_check)
    temp_zip_path.replace(final_zip_path)
    # Inputs are removed by the caller only after final_done is persisted.
    return volume_files


def group_compress(dest_root, group_size, password, volume_size=None,
                   bandizip_path='bandizip', keep_files=False, double_compress=True,
                   auto_close=True, on_progress=None, cancel_check=None,
                   sort_by='name', archive_suffix='.zipp', first_suffix='-First',
                   enable_volume=True, keep_hierarchy=False, folder_order=None,
                   verify=False, first_suffix_replace=None, only_dirs=None,
                   checkpoint=None, force_cancel_check=None, archive_format='zip', stats=None):
    dest_root = Path(dest_root)
    resume_mode = checkpoint is not None
    if not resume_mode and _checkpoint_path(dest_root).exists():
        raise ValueError(tr('输出目录已有断点，请恢复原任务或更换输出目录'))

    if resume_mode:
        if Path(checkpoint).parent.resolve() != dest_root.resolve():
            raise ValueError(tr('恢复输出目录必须与断点所在目录一致'))
        data = _load_checkpoint(checkpoint)
        cfg = data.get('config', {})
        archive_format = cfg.get('archive_format', 'zip')
        group_size = cfg.get('group_size', group_size)
        volume_size = cfg.get('volume', volume_size)
        double_compress = cfg.get('double_compress', double_compress)
        sort_by = cfg.get('sort_by', sort_by)
        archive_suffix = cfg.get('archive_suffix', archive_suffix)
        first_suffix = cfg.get('first_suffix', first_suffix)
        enable_volume = cfg.get('enable_volume', enable_volume)
        first_suffix_replace = cfg.get('first_suffix_replace', first_suffix_replace)
        verify = cfg.get('verify', verify)
        keep_hierarchy = cfg.get('keep_hierarchy', keep_hierarchy)
        keep_files = cfg.get('keep_files', keep_files)
        groups = data.get('groups', [])
    else:
        if not enable_volume:
            volume_size = None
        else:
            first_suffix_replace = None
        if not double_compress:
            archive_suffix = '.' + archive_format
        validate_compression(dict(archive_format=archive_format, password=password, enable_volume=enable_volume,
                                  group_size=group_size, double_compress=double_compress, volume=volume_size))
        folders = _collect_dirs(dest_root, keep_hierarchy, only_dirs)
        all_groups = []
        for folder in folders:
            if folder_order and folder in folder_order:
                files = folder_order[folder]
                files = [f for f in files if f.exists() and f.name.casefold() != RESUME_FILE_NAME]
            else:
                files = [f for f in folder.iterdir() if f.is_file() and f.name.casefold() != RESUME_FILE_NAME]
                files = [f for f in files if f.name != 'List.txt' and f.name.casefold() != RESUME_FILE_NAME]
                _sort_files(files, sort_by)
            for i in range(0, len(files), group_size):
                all_groups.append((folder, files[i:i+group_size], i))
        groups = _build_group_plan(dest_root, folders, all_groups)
        cfg = _resume_config_snapshot(
            group_size, password, volume_size, bandizip_path,
            double_compress, sort_by, archive_suffix,
            first_suffix, enable_volume, first_suffix_replace,
            verify, keep_hierarchy, archive_format)
        cfg['keep_files'] = keep_files

    if enable_volume:
        first_suffix_replace = None
    else:
        volume_size = None
    validate_compression(dict(cfg, first_compress=True, archive_format=archive_format, password=password, group_size=group_size,
                              enable_volume=enable_volume, double_compress=double_compress))
    data = {"app_version": __version__, "config": cfg, "groups": groups}
    failed_groups, skipped_groups = set(), set()
    success_count = sum(g['state'] == 'final_done' for g in groups)
    def finish(result):
        if stats is not None:
            successful = success_count
            failed = len(failed_groups)
            skipped = len(skipped_groups)
            stats.update(groups=successful, groups_total=len(groups), groups_success=successful,
                         groups_failed=failed, groups_skipped=skipped,
                         groups_pending=max(0, len(groups) - successful - failed - skipped))
        return result
    finish('running')
    def persist(group, state):
        nonlocal success_count
        previous = group['state']
        group['state'] = state
        try:
            _write_checkpoint(dest_root, data)
        except OSError:
            group['state'] = previous
            raise
        if state == 'final_done' and previous != 'final_done':
            success_count += 1
    _validate_checkpoint(data, dest_root)
    # Never allow archive cleanup or creation to consume a source of any group.
    source_paths = {str((dest_root / g['folder'] / name).resolve()).casefold()
                    for g in groups for name in g['files']}
    leftovers = _index_artifacts({dest_root / g['folder'] for g in groups}, first_suffix, first_suffix_replace, archive_format)
    for g in groups:
        folder = dest_root / g['folder']
        prefix = g['base_name'] + first_suffix
        output_names = [prefix + '.' + archive_format, f"最终压缩{g['base_name']}.{archive_format}"]
        if first_suffix_replace:
            output_names.append(prefix + first_suffix_replace)
        if double_compress:
            output_names.append(g['base_name'] + archive_suffix)
        outputs = [folder / name for name in output_names]
        if folder.is_dir():
            outputs += leftovers.get((folder, g['base_name']), [])
        for output in outputs:
            if not output.resolve().is_relative_to(dest_root.resolve()) or str(output.resolve()).casefold() in source_paths:
                raise ValueError(tr('压缩产物与源文件冲突或路径越界: {0}', output))
    if not resume_mode:
        data['created_at'] = datetime.now().isoformat(timespec="seconds")
        _write_checkpoint(dest_root, data)
    total = len(groups)
    status = "completed"
    for idx, g in enumerate(groups, 1):
        if _check_cancel(cancel_check):
            return finish(_cancel_status(force_cancel_check))
        if g['state'] == 'final_done':
            continue
        folder, base_name = dest_root / g['folder'], g['base_name']
        if on_progress:
            on_progress(success_count, total, tr('压缩 ({0}/{1}): {2}', idx, total, base_name))
        try:
            first_verified = False
            if g['state'] == 'pending':
                files = _find_group_files(dest_root, g)
                if len(files) != len(g['files']) or any(not _source_matches(f, g) for f in files):
                    print(tr('警告: 组 {0} 源文件缺失或发生变化，保留待处理状态并跳过', base_name))
                    status = "partial"
                    skipped_groups.add(idx)
                    finish(status)
                    continue
                for stale in leftovers.get((folder, base_name), []):
                    stale.unlink()
                first = _compress_first(folder, base_name, files, password, bandizip_path, volume_size,
                                        enable_volume, first_suffix, first_suffix_replace,
                                        force_cancel_check, archive_format)
                if not first.is_file():
                    raise OSError(tr('一次压缩产物缺失: {0}', first))
                if verify:
                    _verify_archive(first, password, bandizip_path, force_cancel_check)
                    first_verified = True
                if any(not _source_matches(source, g) for source in files):
                    raise ValueError(tr('源文件在压缩期间发生变化，已保留输入和断点，请核对后重试'))
                persist(g, 'first_done' if double_compress else 'final_done')
                if not keep_files:
                    for source in files:
                        if not _source_matches(source, g):
                            raise ValueError(tr('源文件在清理前发生变化，已保留：{0}', source))
                        source.unlink()
                if not double_compress:
                    finish(status)
                    if on_progress:
                        on_progress(stats['groups'] if stats is not None else success_count, total, tr('压缩完成: {0}', base_name))
                    continue
                if _check_cancel(cancel_check):
                    return finish(_cancel_status(force_cancel_check))
            # Also verify old first_done checkpoints before creating a second archive.
            if verify and not first_verified:
                first = folder / (base_name + first_suffix +
                                  (first_suffix_replace if first_suffix_replace and not enable_volume else '.' + archive_format))
                if archive_format == '7z' and enable_volume and not first.exists():
                    first = first.with_name(first.name + '.001')
                _verify_archive(first, password, bandizip_path, force_cancel_check)
            artifacts = _compress_second(folder, base_name, first_suffix, password, bandizip_path,
                                         archive_suffix, force_cancel_check,
                                         first_suffix_replace, verify, archive_format)
            persist(g, 'final_done')
            for artifact in artifacts:
                artifact.unlink()
            # A restored first_done checkpoint does not prove that a file now at
            # the old path is still the old input. Never delete recreated files.
            if not keep_files and _find_group_files(dest_root, g):
                print(tr('组 {0} 的遗留源文件已保留，请核对后自行处理', base_name))
            finish(status)
            if on_progress:
                on_progress(success_count, total, tr('压缩完成: {0}', base_name))
        except ForceCancelled:
            print(tr('组 {0} 已强制中断，保留断点及可用输入', base_name))
            return finish("force_cancelled")
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            print(tr('组 {0} 处理失败，保留断点及可用输入: {1}', base_name, redact(error, password)))
            if g['state'] != 'final_done':
                failed_groups.add(idx)
            return finish("error")
    if status == "completed":
        _delete_checkpoint(dest_root)
    return finish(status)


def _resume_task(checkpoint_path, password, bandizip_path, on_progress, cancel_check,
                 force_cancel_check, stats=None):
    data = _load_checkpoint(checkpoint_path)
    cfg = data.get('config', {})
    groups = data.get('groups', [])
    pending = sum(1 for g in groups if g.get('state') != 'final_done')
    print(tr('=== 恢复任务 ==='))
    print(tr('断点文件: {0}', checkpoint_path))
    print(tr('待处理组数: {0} / {1}', pending, len(groups)))
    print(tr('分组大小: {0}', cfg.get('group_size')))
    print(tr('密码: {0}', tr('已设置') if password else tr('无')))
    print(tr('分卷: {0}', cfg.get('volume') or tr('自动检测')))
    print(tr('二次打包: {0}', tr('开启') if cfg.get('double_compress', True) else tr('关闭')))
    print("=" * 40)
    if pending <= 0:
        print(tr('所有组均已完成，无需恢复。'))
        _delete_checkpoint(Path(checkpoint_path).parent)
        if stats is not None:
            stats.update(groups=len(groups), groups_total=len(groups), groups_success=len(groups),
                         groups_failed=0, groups_skipped=0, groups_pending=0)
        return "completed"
    dest_root = Path(checkpoint_path).parent
    from sortzip_core.resume_passwords import verify_legacy
    try:
        if not verify_legacy(dest_root,cfg,groups,bandizip_path,password,cancel_check,force_cancel_check):
            return _cancel_status(force_cancel_check)
    except ForceCancelled:
        return 'force_cancelled'
    if on_progress:
        on_progress(0, 100, 0, 1, tr('准备恢复...'))
    return group_compress(
        dest_root=str(dest_root),
        group_size=cfg.get('group_size', 1),
        password=password,
        volume_size=cfg.get('volume'),
        bandizip_path=bandizip_path,
        keep_files=False,
        double_compress=cfg.get('double_compress', True),
        on_progress=lambda c, t, m: on_progress(0, 100, c, t, m) if on_progress else None,
        cancel_check=cancel_check,
        sort_by=cfg.get('sort_by', 'name'),
        archive_suffix=cfg.get('archive_suffix', '.zipp'),
        first_suffix=cfg.get('first_suffix', '-First'),
        enable_volume=cfg.get('enable_volume', True),
        keep_hierarchy=cfg.get('keep_hierarchy', False),
        verify=cfg.get('verify', False),
        first_suffix_replace=cfg.get('first_suffix_replace'),
        checkpoint=checkpoint_path,
        force_cancel_check=force_cancel_check,
        archive_format=cfg.get('archive_format', 'zip'), stats=stats,
    )


def main_from_config(config, on_progress=None, cancel_check=None, force_cancel_check=None, stats=None):
    if stats is None:
        stats = {}
    stats.pop('error_detail',None)
    stats.update(files_moved=0, files_renamed=0, groups=0, groups_total=0,
                 groups_success=0, groups_failed=0, groups_skipped=0, groups_pending=0)
    if config.get('resume_from'):
        header = json.loads(Path(config['resume_from']).read_text(encoding='utf-8'))
        if isinstance(header,dict) and header.get('schema')==4:
            from sortzip_core.task_recovery import resume
            return resume(config,stats,on_progress,cancel_check,force_cancel_check)
        if isinstance(header, dict) and header.get('kind')=='multilayer':
            from sortzip_core.nested_pipeline import resume
            return resume(config, stats, on_progress, cancel_check, force_cancel_check)
        if config.get('_archive_tool_identity') is not None:
            from sortzip_core.nested_pipeline import preflight_tool
            preflight_tool(config)
        return _resume_task(config['resume_from'], config.get('password', ''),
                            config.get('bandizip', 'bandizip'), on_progress,
                            cancel_check, force_cancel_check, stats)
    config = prepare_config(config)
    if '_nested_plan' in config:
        from sortzip_core.nested_pipeline import preflight_tool
        preflight_tool(config)
    plan = config['execution_plan']
    if not plan:
        print(tr('没有匹配的文件'))
        return 'completed'
    if config.get('first_compress', True):
        count = (len(config['_nested_plan'].tasks) if '_nested_plan' in config
                 else len({(e['folder'], e['group']) for e in plan}))
        stats.update(groups_total=count, groups_pending=count)
    print(tr('执行计划: {0} 个文件；真实压缩格式: {1}', len(plan), config.get('archive_format', 'zip')))
    # Validate the confirmed snapshot before the first copy/move.
    for entry in plan:
        source = entry['source']
        info = source.stat()
        if info.st_size != entry['size'] or info.st_mtime_ns != entry['mtime_ns']:
            raise ValueError(tr('源文件在预览后发生变化，请重新预览：{0}', entry['source_rel']))
        target = Path(config['dest']) / entry['folder'] / entry['classified_name']
        if target.exists() or target.is_symlink():
            raise FileExistsError(tr('分类目标已存在：{0}/{1}', entry['folder'], entry['classified_name']))
    if _check_cancel(cancel_check):
        return _cancel_status(force_cancel_check)
    if '_nested_plan' in config or (not config.get('first_compress',True) and 'archive_tool' in config):
        from sortzip_core.task_recovery import start
        return start(config,stats,on_progress,cancel_check,force_cancel_check)
    def report(start, end, cur, total, message):
        if on_progress:
            on_progress(start, end, cur, total, message)
    def classified(cur, total, message):
        stats['files_moved'] = cur
        report(0, 30, cur, total, message)
    created_dirs = classify_files(config['src'], config['dest'], keep_files=config.get('keep_files', False),
                                 on_progress=classified, cancel_check=cancel_check, execution_plan=plan)
    if _check_cancel(cancel_check):
        return _cancel_status(force_cancel_check)
    folder_order = rename_files_in_folders(config['dest'], config.get('sort_by', 'name'),
        naming_rules=config.get('naming_rules'), keep_hierarchy=config.get('keep_hierarchy', False),
        only_dirs=created_dirs, execution_plan=plan, on_progress=lambda c,t,m: report(30,40,c,t,m), cancel_check=cancel_check, stats=stats)
    if _check_cancel(cancel_check):
        return _cancel_status(force_cancel_check)
    if config.get('output_list') and '_nested_plan' not in config:
        write_rename_list(config['dest'], config.get('naming_rules'),
                          compression_enabled=config.get('first_compress', True), execution_plan=plan)
    if not config.get('first_compress', True):
        return 'completed'
    if '_nested_plan' in config:
        from sortzip_core.nested_pipeline import execute
        return execute(config, stats, on_progress, cancel_check, force_cancel_check)
    return group_compress(config['dest'], config['group_size'], config.get('password', ''),
        volume_size=config.get('volume'), bandizip_path=config.get('bandizip', 'bandizip'),
        double_compress=config.get('double_compress', True),
        on_progress=lambda c, t, m: report(40, 100, c, t, m), cancel_check=cancel_check,
        first_suffix='-First' if config.get('double_compress', True) else '',
        archive_suffix=final_suffix(dict(config, double_compress=True)),
        enable_volume=config.get('enable_volume', True), keep_hierarchy=config.get('keep_hierarchy', False),
        folder_order=folder_order, verify=config.get('verify_archive', False),
        first_suffix_replace=config.get('first_suffix_replace'), only_dirs=created_dirs,
        force_cancel_check=force_cancel_check, archive_format=config.get('archive_format', 'zip'), stats=stats)


def cli():
    main_from_config({
        'src': r'',
        'dest': r'',
        'group_size': 1,
        'password': '',
        'volume': None,
        'bandizip': 'bandizip',
        'custom_names': {},
        'sort_by': 'name',
        'keep_files': False,
        'double_compress': False,
        'first_compress': False,
        'enable_volume': False,
        'auto_close': True,
    })


if __name__ == '__main__':
    cli()
