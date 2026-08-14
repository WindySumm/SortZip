import json
import re
import shutil
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path

from sortzip_core.constants import RESUME_FILE_NAME
from sortzip_core import __version__

SEP = "\t"


class ForceCancelled(Exception):
    pass


def _run_proc(cmd, force_cancel_check=None, wait_ms=200):
    """Run a subprocess, polling every `wait_ms` ms for force-cancel."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=wait_ms / 1000.0)
            break
        except subprocess.TimeoutExpired:
            if force_cancel_check and force_cancel_check():
                proc.kill()
                proc.communicate()
                raise ForceCancelled()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return stdout, stderr


def _natural_key(f):
    parts = re.split(r'(\d+)', f.name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _check_cancel(cancel_check):
    if cancel_check and cancel_check():
        print("用户取消了操作")
        return True
    return False


SORT_FUNCS = {
    'name':        _natural_key,
    'name_desc':   _natural_key,
    'mtime':       lambda f: f.stat().st_mtime,
    'mtime_desc':  lambda f: (-f.stat().st_mtime, f.name),
    'size_asc':    lambda f: f.stat().st_size,
    'size_desc':   lambda f: (-f.stat().st_size, f.name),
    'ext':         lambda f: f.suffix,
}


def _sort_files(files, sort_by):
    fn = SORT_FUNCS.get(sort_by)
    if fn is None:
        fn = SORT_FUNCS['name']
    reverse = sort_by == 'name_desc'
    files.sort(key=fn, reverse=reverse)


def _dedup_name(dest_dir, stem, ext):
    name = f"{stem}{ext}"
    counter = 0
    while (dest_dir / name).exists():
        counter += 1
        name = f"{stem}_{counter}{ext}"
    return name


def classify_files(src_dir, dest_root, custom_names=None, on_progress=None, cancel_check=None,
                   keep_files=False, recursive=False, keep_hierarchy=False):
    src_path = Path(src_dir)
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    if recursive:
        files = [f for f in src_path.rglob('*') if f.is_file() and f.name != RESUME_FILE_NAME]
    else:
        files = [f for f in src_path.iterdir() if f.is_file() and f.name != RESUME_FILE_NAME]
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
            print(f"复制: {file_path.name} -> {dest_path}")
        else:
            shutil.move(str(file_path), str(dest_path))
            print(f"移动: {file_path.name} -> {dest_path}")
        if on_progress:
            on_progress(idx, total, f"分类: {file_path.name}")
    return created_dirs


def render_template(template, idx, ext, folder_name, original_name):
    if not template:
        return None
    return template.replace('{n}', str(idx)) \
                   .replace('{ext}', ext) \
                   .replace('{folder}', folder_name) \
                   .replace('{original}', original_name)


def _match_rule(naming_rules, folder_name):
    if not naming_rules:
        return None
    for rule in naming_rules:
        if not rule.get('enable', True):
            continue
        match = rule.get('match_folder', '')
        if match == '*' or match == folder_name:
            return rule
    return None


def _match_rule_hierarchy(naming_rules, folder, root=None):
    """Walk up ancestors to find a matching naming rule."""
    if not naming_rules:
        return None
    walk = folder
    root = Path(root) if root else None
    while True:
        rule = _match_rule(naming_rules, walk.name)
        if rule:
            return rule
        if root is not None and walk == root:
            break
        if walk.parent == walk:
            break
        walk = walk.parent
    return None


def check_naming_conflicts(folder_name, files, template):
    conflicts = []
    seen = {}
    for idx, file_path in enumerate(files, start=1):
        new_name = render_template(template, idx, file_path.suffix,
                                   folder_name, file_path.stem)
        if new_name in seen:
            conflicts.append((seen[new_name], file_path.name, new_name))
        else:
            seen[new_name] = file_path.name
    return conflicts


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
                      compression_enabled=True, keep_hierarchy=False, only_dirs=None):
    if compression_enabled:
        COL_W = (4, 24, 24, 24)
        HEADERS = ("序号", "原文件名", "新文件名", "所属压缩包名")
    else:
        COL_W = (4, 24, 24)
        HEADERS = ("序号", "原文件名", "新文件名")
    dest_root = Path(dest_root)
    for folder in _collect_dirs(dest_root, keep_hierarchy, only_dirs):
        files = [f for f in folder.iterdir() if f.is_file() and f.name != RESUME_FILE_NAME]
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
        print(f"已输出命名对照表: {list_path}")


def rename_files_in_folders(dest_root, sort_by='name', on_progress=None, cancel_check=None,
                            naming_rules=None, keep_hierarchy=False, preview_order=None, only_dirs=None):
    dest_root = Path(dest_root)
    folders = _collect_dirs(dest_root, keep_hierarchy, only_dirs)
    done = 0
    total = 0
    for folder in folders:
        total += len([f for f in folder.iterdir() if f.is_file() and f.name not in ('List.txt', RESUME_FILE_NAME)])
    folder_order = {}
    for folder in folders:
        if _check_cancel(cancel_check):
            return None
        files = [f for f in folder.iterdir() if f.is_file() and f.name not in ('List.txt', RESUME_FILE_NAME)]
        if not files:
            continue
        if preview_order and folder.name in preview_order:
            ordered = preview_order[folder.name]
            name_map = {f.name: f for f in files}
            files = [name_map[n] for n in ordered if n in name_map]
            files += [f for f in files if f.name not in ordered]
        else:
            _sort_files(files, sort_by)
        if keep_hierarchy:
            rule = _match_rule_hierarchy(naming_rules, folder, dest_root) if naming_rules else None
        else:
            rule = _match_rule(naming_rules, folder.name) if naming_rules else None
        if not rule:
            print(f"跳过重命名（未匹配规则）: {folder.name}")
            done += len(files)
            folder_order[folder] = files[:]
            continue
        template = rule.get('template', '')
        rename_map = {}
        renamed_order = []
        for idx, file_path in enumerate(files, start=1):
            new_name = render_template(template, idx, file_path.suffix,
                                       folder.name, file_path.stem)
            if not new_name:
                continue
            new_path = folder / new_name
            rename_map[file_path] = new_path
        for src, dst in rename_map.items():
            if _check_cancel(cancel_check):
                return None
            if src == dst:
                print(f"跳过（已命名正确）: {src.name}")
                done += 1
                continue
            if dst.exists():
                temp_name = dst.name + ".tmp"
                temp_path = folder / temp_name
                counter = 1
                while temp_path.exists():
                    temp_name = f"{dst.stem}.tmp{counter}{dst.suffix}"
                    temp_path = folder / temp_name
                    counter += 1
                dst.rename(temp_path)
                print(f"临时移动: {dst.name} -> {temp_name}")
            src.rename(dst)
            done += 1
            renamed_order.append(dst)
            print(f"重命名: {src.name} -> {dst.name}")
            if on_progress:
                on_progress(done, total, f"重命名: {src.name}")
        folder_order[folder] = renamed_order
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


# ---- 断点续传 ----

def _checkpoint_path(dest_root):
    return Path(dest_root) / RESUME_FILE_NAME


def _write_checkpoint(dest_root, data):
    path = _checkpoint_path(dest_root)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        print(f"警告: 写入断点文件失败 {path}: {e}")


def _load_checkpoint(path):
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (OSError, ValueError) as e:
        raise ValueError(f"断点文件无法读取或已损坏: {path}") from e


def _delete_checkpoint(dest_root):
    path = _checkpoint_path(dest_root)
    try:
        if path.exists():
            path.unlink()
            print(f"断点文件已清除: {path}")
    except OSError as e:
        print(f"警告: 清除断点文件失败 {path}: {e}")


def _resume_config_snapshot(group_size, password, volume_size, bandizip_path,
                            double_compress, auto_close, sort_by, archive_suffix,
                            first_suffix, enable_volume, first_suffix_replace,
                            verify, keep_hierarchy):
    return {
        "group_size": group_size,
        "password": bool(password),
        "volume": volume_size,
        "bandizip": bandizip_path,
        "double_compress": double_compress,
        "auto_close": auto_close,
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
        groups.append({
            "folder": rel,
            "base_name": base_name,
            "files": [f.name for f in group],
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


def _compress_first(folder, base_name, files, password, bandizip_path, volume_size,
                    auto_close, enable_volume, first_suffix, first_suffix_replace,
                    force_cancel_check):
    first_name = f"{base_name}{first_suffix}"
    zip_path = folder / f"{first_name}.zip"
    auto_vol = None
    if enable_volume:
        if volume_size is None:
            total_bytes = sum(f.stat().st_size for f in files)
            auto_vol = get_auto_volume(total_bytes)
            print(f"  组 {base_name} 总大小: {total_bytes / (1024**3):.2f} GB，自动分卷大小 = {auto_vol}")
        else:
            auto_vol = None
    cmd = [bandizip_path, 'a']
    if password:
        cmd.extend(['-p:' + password])
    if enable_volume and volume_size:
        cmd.extend(['-v:' + volume_size])
    elif enable_volume and auto_vol:
        cmd.extend(['-v:' + auto_vol])
    if auto_close:
        cmd.append('-y')
    cmd.append(str(zip_path))
    cmd.extend([str(f) for f in files])
    print(f"第一次压缩: {' '.join(cmd)}")
    _run_proc(cmd, force_cancel_check)
    print(f"成功创建分卷: {zip_path} (及其分卷)")
    if first_suffix_replace and not enable_volume:
        if zip_path.exists():
            new_zip_path = folder / f"{first_name}{first_suffix_replace}"
            if new_zip_path.exists():
                new_zip_path.unlink()
            zip_path.rename(new_zip_path)
            zip_path = new_zip_path
            print(f"一次压缩后缀替换: {zip_path.name}")
    return zip_path


def _compress_second(folder, base_name, first_suffix, password, bandizip_path,
                     auto_close, archive_suffix, force_cancel_check):
    first_name = f"{base_name}{first_suffix}"
    volume_files = list(folder.glob(f"{first_name}.*"))
    volume_files = [f for f in volume_files if f.name != f"最终压缩{base_name}.zip"]
    if not volume_files:
        print(f"警告: 未找到分卷文件，跳过二次打包")
        return
    temp_zip_name = f"最终压缩{base_name}.zip"
    temp_zip_path = folder / temp_zip_name
    final_zip_name = f"{base_name}{archive_suffix}"
    final_zip_path = folder / final_zip_name
    cmd2 = [bandizip_path, 'a']
    if password:
        cmd2.extend(['-p:' + password])
    if auto_close:
        cmd2.append('-y')
    cmd2.append(str(temp_zip_path))
    cmd2.extend([str(f) for f in volume_files])
    print(f"二次打包: {' '.join(cmd2)}")
    _run_proc(cmd2, force_cancel_check)
    print(f"成功创建二次打包: {temp_zip_path}")
    for f in volume_files:
        f.unlink()
        print(f"已删除分卷文件: {f}")
    temp_zip_path.rename(final_zip_path)
    print(f"重命名: {temp_zip_name} -> {final_zip_name}")


def group_compress(dest_root, group_size, password, volume_size=None,
                   bandizip_path='bandizip', keep_files=False, double_compress=True,
                   auto_close=True, on_progress=None, cancel_check=None,
                   sort_by='name', archive_suffix='.zipp', first_suffix='-First',
                   enable_volume=True, keep_hierarchy=False, folder_order=None,
                   verify=False, first_suffix_replace=None, only_dirs=None,
                   checkpoint=None, force_cancel_check=None):
    dest_root = Path(dest_root)
    resume_mode = checkpoint is not None

    if resume_mode:
        data = _load_checkpoint(checkpoint)
        cfg = data.get('config', {})
        group_size = cfg.get('group_size', group_size)
        volume_size = cfg.get('volume', volume_size)
        bandizip_path = cfg.get('bandizip', bandizip_path)
        double_compress = cfg.get('double_compress', double_compress)
        auto_close = cfg.get('auto_close', auto_close)
        sort_by = cfg.get('sort_by', sort_by)
        archive_suffix = cfg.get('archive_suffix', archive_suffix)
        first_suffix = cfg.get('first_suffix', first_suffix)
        enable_volume = cfg.get('enable_volume', enable_volume)
        first_suffix_replace = cfg.get('first_suffix_replace', first_suffix_replace)
        verify = cfg.get('verify', verify)
        keep_hierarchy = cfg.get('keep_hierarchy', keep_hierarchy)
        groups = data.get('groups', [])
    else:
        folders = _collect_dirs(dest_root, keep_hierarchy, only_dirs)
        all_groups = []
        for folder in folders:
            if folder_order and folder in folder_order:
                files = folder_order[folder]
                files = [f for f in files if f.exists() and f.suffix.lower() != '.zip'
                         and f.name != 'List.txt' and f.name != RESUME_FILE_NAME]
            else:
                files = [f for f in folder.iterdir() if f.is_file() and f.name != RESUME_FILE_NAME]
                files = [f for f in files if f.suffix.lower() != '.zip'
                         and f.name != 'List.txt' and f.name != RESUME_FILE_NAME]
                _sort_files(files, sort_by)
            for i in range(0, len(files), group_size):
                all_groups.append((folder, files[i:i+group_size], i))
        groups = _build_group_plan(dest_root, folders, all_groups)
        cfg = _resume_config_snapshot(
            group_size, password, volume_size, bandizip_path,
            double_compress, auto_close, sort_by, archive_suffix,
            first_suffix, enable_volume, first_suffix_replace,
            verify, keep_hierarchy)
        _write_checkpoint(dest_root, {
            "app_version": __version__,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "config": cfg,
            "groups": groups,
        })

    total = len(groups)
    completed = True
    for idx, g in enumerate(groups, start=1):
        if _check_cancel(cancel_check):
            completed = False
            break
        if g.get('state') == 'final_done':
            continue
        folder = Path(dest_root) / g['folder']
        base_name = g['base_name']
        if on_progress:
            on_progress(idx, total, f"压缩 ({idx}/{total}): {base_name}")
        if g.get('state') == 'first_done':
            temp_zip = folder / f"最终压缩{base_name}.zip"
            if temp_zip.exists():
                temp_zip.unlink()
            for orphan in _find_group_files(dest_root, g):
                try:
                    orphan.unlink()
                    print(f"清理孤儿源文件: {orphan}")
                except OSError as e:
                    print(f"警告: 无法清理孤儿源文件 {orphan}: {e}")
            try:
                _compress_second(folder, base_name, first_suffix, password, bandizip_path,
                                 auto_close, archive_suffix, force_cancel_check)
            except (subprocess.CalledProcessError, FileNotFoundError, ForceCancelled) as e:
                print(f"续传二次打包未完成: {e}")
                completed = False
                break
            g['state'] = 'final_done'
            _write_checkpoint(dest_root, {
                "app_version": __version__,
                "config": cfg,
                "groups": groups,
            })
            continue
        files = _find_group_files(dest_root, g)
        if not files:
            print(f"警告: 组 {base_name} 源文件缺失，跳过")
            g['state'] = 'final_done'
            continue
        for stale in folder.glob(f"{base_name}{first_suffix}.*"):
            if stale.name != f"最终压缩{base_name}.zip":
                try:
                    stale.unlink()
                    print(f"已删除残留归档: {stale.name}")
                except OSError as e:
                    print(f"警告: 无法删除残留 {stale}: {e}")
        try:
            _compress_first(folder, base_name, files, password, bandizip_path, volume_size,
                            auto_close, enable_volume, first_suffix, first_suffix_replace,
                            force_cancel_check)
        except ForceCancelled:
            print("已强制取消（一次压缩被终止），源文件未删除，可续传恢复。")
            completed = False
            break
        except subprocess.CalledProcessError as e:
            print(f"第一次压缩失败: {e.stderr}")
            completed = False
            break
        except FileNotFoundError:
            print(f"错误: 找不到可执行文件 '{bandizip_path}'，请确保已安装并加入PATH。")
            completed = False
            break
        g['state'] = 'first_done' if double_compress else 'final_done'
        _write_checkpoint(dest_root, {
            "app_version": __version__,
            "config": cfg,
            "groups": groups,
        })
        if not keep_files:
            for f in files:
                try:
                    f.unlink()
                    print(f"已删除原始文件: {f}")
                except OSError as e:
                    print(f"警告: 无法删除 {f}: {e}")
        if double_compress:
            if _check_cancel(cancel_check):
                completed = False
                break
            try:
                _compress_second(folder, base_name, first_suffix, password, bandizip_path,
                                 auto_close, archive_suffix, force_cancel_check)
            except ForceCancelled:
                print("已强制取消（二次打包被终止），源文件已删除，断点为 first_done。")
                completed = False
                break
            except (subprocess.CalledProcessError, OSError) as e:
                print(f"二次打包未完成: {e}，保留分卷文件")
                completed = False
                break
            except FileNotFoundError:
                print(f"错误: 找不到可执行文件 '{bandizip_path}'，请确保已安装并加入PATH。")
                completed = False
                break
            g['state'] = 'final_done'
            _write_checkpoint(dest_root, {
                "app_version": __version__,
                "config": cfg,
                "groups": groups,
            })
        else:
            print("跳过二次打包（已关闭）")

    if verify and completed:
        print("\n开始校验压缩包完整性...")
        verify_folders = set()
        for g in groups:
            verify_folders.add(Path(dest_root) / g['folder'])
        for folder in sorted(verify_folders, key=lambda d: d.relative_to(dest_root)):
            archives = [f for f in folder.iterdir() if f.is_file()
                        and f.suffix.lower() in ('.zip', '.7z', '.rar', '.tar', '.gz',
                                                 '.bz2', '.xz', '.lzh', '.alz', '.egg', '.zipp')]
            for arch in archives:
                if _check_cancel(cancel_check):
                    return
                test_cmd = [bandizip_path, 't']
                if password:
                    test_cmd.append('-p:' + password)
                if auto_close:
                    test_cmd.append('-y')
                test_cmd.append(str(arch))
                print(f"校验: {arch.name}")
                try:
                    _run_proc(test_cmd, force_cancel_check)
                    print(f"  ✔ {arch.name} 校验通过")
                except subprocess.CalledProcessError:
                    print(f"  ✘ {arch.name} 校验失败，文件可能已损坏")
                except FileNotFoundError:
                    print(f"  ✘ 找不到 Bandizip，无法校验")
                    return
        print("校验完成。")

    if completed and not _check_cancel(cancel_check):
        _delete_checkpoint(dest_root)


def _resume_task(checkpoint_path, password, bandizip_path, on_progress, cancel_check,
                 force_cancel_check):
    data = _load_checkpoint(checkpoint_path)
    cfg = data.get('config', {})
    groups = data.get('groups', [])
    pending = sum(1 for g in groups if g.get('state') != 'final_done')
    print("=== 断点续传 ===")
    print(f"断点文件: {checkpoint_path}")
    print(f"待处理组数: {pending} / {len(groups)}")
    print(f"分组大小: {cfg.get('group_size')}")
    print(f"密码: {'已设置' if password else '无'}")
    print(f"分卷: {cfg.get('volume') or '自动检测'}")
    print(f"二次打包: {'开启' if cfg.get('double_compress', True) else '关闭'}")
    print("=" * 40)
    if pending <= 0:
        print("所有组均已完成，无需续传。")
        return
    dest_root = Path(checkpoint_path).parent
    if on_progress:
        on_progress(0, 100, 0, 1, "准备续传...")
    group_compress(
        dest_root=str(dest_root),
        group_size=cfg.get('group_size', 1),
        password=password,
        volume_size=cfg.get('volume'),
        bandizip_path=bandizip_path,
        keep_files=False,
        double_compress=cfg.get('double_compress', True),
        auto_close=cfg.get('auto_close', True),
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
    )


def main_from_config(config, on_progress=None, cancel_check=None, force_cancel_check=None):
    resume_from = config.get('resume_from')
    if resume_from:
        _resume_task(resume_from, config.get('password', ''), config.get('bandizip', 'bandizip'),
                     on_progress, cancel_check, force_cancel_check)
        return
    print("=== 使用配置参数运行 ===")
    print(f"输入文件夹: {config['src']}")
    print(f"输出文件夹: {config['dest']}")
    print(f"每包文件数: {config['group_size']}")
    print(f"密码: {'已设置' if config['password'] else '无'}")
    print(f"分卷: {'自动检测' if config['volume'] is None else config['volume']}")
    print(f"压缩工具: {config['bandizip']}")
    print(f"自定义分类: {config['custom_names']}")
    sort_labels = {'name': '文件名(升序)', 'name_desc': '文件名(降序)', 'mtime': '修改时间(旧→新)',
                   'mtime_desc': '修改时间(新→旧)', 'size_asc': '文件大小(小→大)',
                   'size_desc': '文件大小(大→小)', 'ext': '扩展名'}
    print(f"排序依据: {sort_labels.get(config.get('sort_by', 'name'), config.get('sort_by', 'name'))}")
    print(f"保留原始文件: {'开启' if config['keep_files'] else '关闭'}")
    print(f"输出目录: {'开启' if config.get('output_list', False) else '关闭'}")
    print(f"二次打包: {'开启' if config.get('double_compress', True) else '关闭'}")
    print(f"自动关闭窗口: {'开启' if config.get('auto_close', True) else '关闭'}")
    print(f"一次压缩: {'开启' if config.get('first_compress', True) else '关闭'}")
    print(f"保持文件夹层级: {'开启' if config.get('keep_hierarchy', False) else '关闭'}")
    print("=" * 40)
    src = config['src']
    dest = config['dest']
    custom_names = config.get('custom_names', {})
    naming_rules = config.get('naming_rules', None)
    keep_hierarchy = config.get('keep_hierarchy', False)
    if _check_cancel(cancel_check):
        return
    print("开始文件分类...")
    created_dirs = classify_files(src, dest, custom_names,
                   on_progress=lambda c, t, m: on_progress(0, 30, c, t, m) if on_progress else None,
                   cancel_check=cancel_check,
                   keep_files=config.get('keep_files', False),
                   recursive=config.get('recursive', False),
                   keep_hierarchy=keep_hierarchy)
    print("分类完成。")
    if _check_cancel(cancel_check):
        return
    if config.get('output_list', False):
        print("输出命名对照表...")
        write_rename_list(dest, naming_rules, config.get('sort_by', 'name'),
                          config.get('group_size', 1), config.get('archive_suffix', '.zip'),
                          compression_enabled=config.get('first_compress', True),
                          keep_hierarchy=keep_hierarchy, only_dirs=created_dirs)
    print("开始重命名...")
    folder_order = rename_files_in_folders(dest, config['sort_by'],
                            on_progress=lambda c, t, m: on_progress(30, 40, c, t, m) if on_progress else None,
                            cancel_check=cancel_check,
                            naming_rules=naming_rules,
                            keep_hierarchy=keep_hierarchy,
                            preview_order=config.get('preview_order'),
                            only_dirs=created_dirs)
    print("重命名完成。")
    if _check_cancel(cancel_check):
        return
    if config.get('first_compress', True):
        print("开始分组压缩...")
        first_suffix = '-First' if config.get('double_compress', True) else ''
        group_compress(
            dest_root=dest,
            group_size=config['group_size'],
            password=config['password'],
            volume_size=config['volume'],
            bandizip_path=config['bandizip'],
            keep_files=False,
            double_compress=config.get('double_compress', True),
            auto_close=config.get('auto_close', True),
            on_progress=lambda c, t, m: on_progress(40, 100, c, t, m) if on_progress else None,
            cancel_check=cancel_check,
            sort_by=config.get('sort_by', 'name'),
            archive_suffix=config.get('archive_suffix', '.zipp'),
            first_suffix=first_suffix,
            enable_volume=config.get('enable_volume', True),
            keep_hierarchy=keep_hierarchy,
            folder_order=folder_order,
            verify=config.get('verify_archive', False),
            first_suffix_replace=config.get('first_suffix_replace'),
            only_dirs=created_dirs,
            force_cancel_check=force_cancel_check,
        )
        print("所有任务完成！")
    else:
        print("压缩已禁用，仅完成分类与重命名。")


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
