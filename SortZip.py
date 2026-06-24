import os
import shutil
import subprocess
import argparse
from pathlib import Path

# ========== 在这里配置参数 ==========
CONFIG = {
    'src': r'E:\测试文件夹',              # 源文件夹路径
    'dest': r'E:\测试输出',           # 目标根目录
    'group_size': 4,                 # 每包文件数
    'password': '12345678',          # 压缩密码（留空表示无密码）密码至少要8位数
    'volume': None,                  # 手动分卷大小（如 '100m'），None 表示自动检测
    'bandizip': 'bandizip',          # 可执行文件名，或 'bz' 或完整路径
    'custom_names': {'.txt': '文档'}, # 扩展名→文件夹名映射（留空则用扩展名）可用,分隔 选择多种类型
    'sort_by': 'name',               # 排序方式 'name' 或 'mtime'
    'keep_files': False,             # True=保留原始文件，False=删除
    'double_compress': True,         # True=二次打包生成 .zipp，False=只保留分卷
    'auto_close': True,              # True=压缩完成自动关闭窗口（加 -y），False=不自动关闭
}
# ===================================


def _check_cancel(cancel_check):
    if cancel_check and cancel_check():
        print("用户取消了操作")
        return True
    return False


# ---- 文件分类：按扩展名将源文件复制/移动到目标子目录 ----
def classify_files(src_dir, dest_root, custom_names=None, on_progress=None, cancel_check=None, keep_files=False):
    src_path = Path(src_dir)
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    files = [f for f in src_path.iterdir() if f.is_file()]
    total = len(files)

    for idx, file_path in enumerate(files, start=1):
        if _check_cancel(cancel_check):
            return
        ext = file_path.suffix.lower()
        if custom_names and ext in custom_names:
            folder_name = custom_names[ext]
        elif custom_names is not None:
            continue
        else:
            folder_name = ext[1:] if ext else 'no_extension'
        target_dir = dest_root / folder_name
        target_dir.mkdir(exist_ok=True)
        if keep_files:
            shutil.copy2(str(file_path), str(target_dir / file_path.name))
            print(f"复制: {file_path.name} -> {target_dir}")
        else:
            shutil.move(str(file_path), str(target_dir / file_path.name))
            print(f"移动: {file_path.name} -> {target_dir}")
        if on_progress:
            on_progress(idx, total, f"分类: {file_path.name}")


# ---- 顺序重命名：将每个目录中的文件按序号重命名 ----
def rename_files_in_folders(dest_root, sort_by='name', on_progress=None, cancel_check=None):
    dest_root = Path(dest_root)
    folders = [f for f in dest_root.iterdir() if f.is_dir()]
    done = 0
    total = 0
    for folder in folders:
        total += len([f for f in folder.iterdir() if f.is_file()])

    for folder in folders:
        if _check_cancel(cancel_check):
            return
        files = [f for f in folder.iterdir() if f.is_file()]
        if not files:
            continue
        if sort_by == 'mtime':
            files.sort(key=lambda f: f.stat().st_mtime)
        else:
            files.sort(key=lambda f: f.name)

        rename_map = {}
        for idx, file_path in enumerate(files, start=1):
            new_name = f"{idx}{file_path.suffix}"
            new_path = folder / new_name
            rename_map[file_path] = new_path

        for src, dst in rename_map.items():
            if _check_cancel(cancel_check):
                return
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
            print(f"重命名: {src.name} -> {dst.name}")
            if on_progress:
                on_progress(done, total, f"重命名: {src.name}")


# ---- 自动计算分卷大小 ----
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


# ---- 分组压缩：调用 Bandizip 对每组文件进行分卷压缩 + 可选二次打包 ----
def group_compress(dest_root, group_size, password, volume_size=None,
                   bandizip_path='bandizip', keep_files=False, double_compress=True,
                   auto_close=True, on_progress=None, cancel_check=None,
                   skip_rename=False, sort_by='name'):
    dest_root = Path(dest_root)
    folders = [f for f in dest_root.iterdir() if f.is_dir()]

    # 统计总组数
    all_groups = []
    for folder in folders:
        files = [f for f in folder.iterdir() if f.is_file()]
        files = [f for f in files if f.suffix.lower() != '.zip']
        if not skip_rename:
            files = [f for f in files if f.stem.isdigit()]
            files.sort(key=lambda f: int(f.stem))
        else:
            if sort_by == 'mtime':
                files.sort(key=lambda f: f.stat().st_mtime)
            else:
                files.sort(key=lambda f: f.name)
        for i in range(0, len(files), group_size):
            all_groups.append((folder, files[i:i+group_size], i))

    total = len(all_groups)
    for idx, (folder, group, start_i) in enumerate(all_groups, start=1):
        if _check_cancel(cancel_check):
            return

        if skip_rename:
            start_num = start_i + 1
            end_num = start_i + len(group)
        else:
            start_num = int(group[0].stem)
            end_num = int(group[-1].stem)
        base_name = f"{start_num}-{end_num}"
        first_name = f"{start_num}-{end_num}-First"
        zip_name = f"{first_name}.zip"
        zip_path = folder / zip_name

        if volume_size is None:
            total_bytes = sum(f.stat().st_size for f in group)
            auto_vol = get_auto_volume(total_bytes)
            print(f"  组 {base_name} 总大小: {total_bytes / (1024**3):.2f} GB，自动分卷大小 = {auto_vol}")
        else:
            auto_vol = None

        cmd = [bandizip_path, 'a']
        if password:
            cmd.extend(['-p:' + password])
        if volume_size:
            cmd.extend(['-v:' + volume_size])
        elif auto_vol:
            cmd.extend(['-v:' + auto_vol])
        if auto_close:
            cmd.append('-y')
        cmd.append(str(zip_path))
        cmd.extend([str(f) for f in group])

        print(f"第一次压缩: {' '.join(cmd)}")
        if on_progress:
            on_progress(idx, total, f"压缩 ({idx}/{total}): {base_name}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            print(f"成功创建分卷: {zip_path} (及其分卷)")
            if not keep_files:
                for f in group:
                    f.unlink()
                    print(f"已删除原始文件: {f}")
        except subprocess.CalledProcessError as e:
            print(f"第一次压缩失败: {e.stderr}")
            continue
        except FileNotFoundError:
            print(f"错误: 找不到可执行文件 '{bandizip_path}'，请确保已安装并加入PATH。")
            return

        if double_compress:
            if _check_cancel(cancel_check):
                return
            volume_files = list(folder.glob(f"{first_name}.*"))
            volume_files = [f for f in volume_files if f.name != f"最终压缩{base_name}.zip"]
            if not volume_files:
                print(f"警告: 未找到分卷文件，跳过二次打包")
                continue

            temp_zip_name = f"最终压缩{base_name}.zip"
            temp_zip_path = folder / temp_zip_name
            final_zip_name = f"{base_name}.zipp"
            final_zip_path = folder / final_zip_name

            cmd2 = [bandizip_path, 'a']
            if password:
                cmd2.extend(['-p:' + password])
            if auto_close:
                cmd2.append('-y')
            cmd2.append(str(temp_zip_path))
            cmd2.extend([str(f) for f in volume_files])

            print(f"二次打包: {' '.join(cmd2)}")
            try:
                subprocess.run(cmd2, check=True, capture_output=True, text=True)
                print(f"成功创建二次打包: {temp_zip_path}")
                for f in volume_files:
                    f.unlink()
                    print(f"已删除分卷文件: {f}")
                temp_zip_path.rename(final_zip_path)
                print(f"重命名: {temp_zip_name} -> {final_zip_name}")
            except subprocess.CalledProcessError as e:
                print(f"二次打包失败: {e.stderr}，保留分卷文件")
            except Exception as e:
                print(f"二次打包过程中发生错误: {e}，保留分卷文件")
        else:
            print("跳过二次打包（已关闭）")


# ---- 从配置字典执行完整任务流程 ----
def main_from_config(config, on_progress=None, cancel_check=None, on_stats=None):
    """从配置字典执行任务"""
    print("=== 使用配置参数运行 ===")
    print(f"源文件夹: {config['src']}")
    print(f"目标根目录: {config['dest']}")
    print(f"每包文件数: {config['group_size']}")
    print(f"密码: {'已设置' if config['password'] else '无'}")
    print(f"分卷: {'自动检测' if config['volume'] is None else config['volume']}")
    print(f"压缩工具: {config['bandizip']}")
    print(f"自定义分类: {config['custom_names']}")
    print(f"排序依据: {config['sort_by']}")
    print(f"保留原始文件: {config['keep_files']}")
    print(f"二次打包: {'开启' if config['double_compress'] else '关闭'}")
    print(f"自动关闭窗口: {'开启' if config.get('auto_close', True) else '关闭'}")
    print("=" * 40)

    src = config['src']
    dest = config['dest']
    custom_names = config.get('custom_names', {})
    skip_rename = config.get('skip_rename', False)

    if _check_cancel(cancel_check):
        return
    print("开始文件分类...")
    classify_files(src, dest, custom_names,
                   on_progress=lambda c, t, m: on_progress(0, 30, c, t, m) if on_progress else None,
                   cancel_check=cancel_check,
                   keep_files=config.get('keep_files', False))
    print("分类完成。")

    if _check_cancel(cancel_check):
        return
    if not skip_rename:
        print("开始重命名...")
        rename_files_in_folders(dest, config['sort_by'],
                                on_progress=lambda c, t, m: on_progress(30, 40, c, t, m) if on_progress else None,
                                cancel_check=cancel_check)
        print("重命名完成。")
    else:
        print("跳过重命名（已关闭）。")

    if _check_cancel(cancel_check):
        return
    print("开始分组压缩...")
    group_compress(
        dest_root=dest,
        group_size=config['group_size'],
        password=config['password'],
        volume_size=config['volume'],
        bandizip_path=config['bandizip'],
        keep_files=False,  # 分类阶段已决定复制/移动，压缩后一律清理
        double_compress=config['double_compress'],
        auto_close=config.get('auto_close', True),
        on_progress=lambda c, t, m: on_progress(40, 100, c, t, m) if on_progress else None,
        cancel_check=cancel_check,
        skip_rename=skip_rename,
        sort_by=config.get('sort_by', 'name'),
    )
    print("所有任务完成！")


# ---- 命令行入口（仅供直接运行 SortZip.py 使用） ----
def main():
    """只使用顶部的 CONFIG 配置运行"""
    main_from_config(CONFIG)


if __name__ == '__main__':
    main()
