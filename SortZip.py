#!/usr/bin/env python3
"""SortZip CLI — thin backward-compatible entry point.

Now delegates to sortzip_core.engine.
"""
from sortzip_core.engine import cli

if __name__ == '__main__':
    cli()
