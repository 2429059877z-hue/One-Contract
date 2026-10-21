#!/usr/bin/env python3
"""Backup/check/restore Studio's external data; never overwrite existing data."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Studio 数据备份、校验与安全迁移（恢复到新目录）')
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='停止其他写入后备份全部外置数据')
    backup.add_argument('--data-dir', required=True, type=Path)
    backup.add_argument('--output', required=True, type=Path)
    check = commands.add_parser('check', help='只读核对备份清单与全部文件 SHA-256')
    check.add_argument('--archive', required=True, type=Path)
    restore = commands.add_parser('restore', help='恢复到尚不存在的新目录，不覆盖既有数据')
    restore.add_argument('--archive', required=True, type=Path)
    restore.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        # Do not import rule_studio.__init__: restore must work without HTTP/schema dependencies.
        spec = importlib.util.spec_from_file_location('studio_data_backup', Path(__file__).parent / 'rule_studio/data_backup.py')
        backup_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup_module)
        create_backup, check_backup, restore_backup = backup_module.create_backup, backup_module.check_backup, backup_module.restore_backup
        if args.command == 'backup':
            result = create_backup(args.data_dir, args.output)
        elif args.command == 'check':
            result = check_backup(args.archive)
        else:
            result = restore_backup(args.archive, args.destination)
        print(json.dumps({'success': True, 'result': result}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
