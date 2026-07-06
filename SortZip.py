#!/usr/bin/env python3
"""SortZip CLI — thin backward-compatible entry point.

Now delegates to sortzip_core.engine.
"""
from sortzip_core.engine import main_from_config, cli

CONFIG = {
    'src': r'E:\测试文件夹',
    'dest': r'E:\测试输出',
    'group_size': 4,
    'password': '12345678',
    'volume': None,
    'bandizip': 'bandizip',
    'custom_names': {'.txt': '文档'},
    'sort_by': 'name',
    'keep_files': False,
    'double_compress': True,
    'auto_close': True,
}


def main():
    cli()


if __name__ == '__main__':
    main()
