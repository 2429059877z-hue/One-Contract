"""Private data-root backup and non-overwriting restore, using only the stdlib.

Archives preserve all stored content and public-version bindings. They do not
publish rules, resolve conflicts, recover transactions, or follow linked files.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import types
import unicodedata
import zipfile

# Recovery must remain usable when the optional Rules/HTTP dependencies are missing.
# Reuse the same audited storage modules without executing the eager package __init__.
if 'one_contract_rules.private_fs' in sys.modules and 'one_contract_rules.transaction_lock' in sys.modules:
    private_fs = sys.modules['one_contract_rules.private_fs']
    profile_lock = sys.modules['one_contract_rules.transaction_lock'].profile_lock
else:
    package_name = '_studio_backup_storage'
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / 'one_contract_rules')]
        sys.modules[package_name] = package
    private_fs = importlib.import_module(package_name + '.private_fs')
    profile_lock = importlib.import_module(package_name + '.transaction_lock').profile_lock

FORMAT = 'one-contract-studio-backup'
MANIFEST = 'backup-manifest.json'
MAX_FILES = 200000
MAX_BYTES = 20 * 1024 ** 3
CHUNK = 1024 * 1024
_LOCK_DIRS = {'.locks', 'clients/.locks'}
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


class BackupError(ValueError):
    pass


def _relative(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value:
        raise BackupError('备份包含不安全的相对路径。')
    parts = value.split('/')
    for part in parts:
        if (part in ('', '.', '..') or any(c in part for c in ':<>"|?*') or '\x00' in part or part.endswith(('.', ' '))
                or any(ord(c) < 32 for c in part) or part.split('.')[0].upper() in _RESERVED):
            raise BackupError('备份路径不可安全迁移到 Windows/macOS。')
    if str(PurePosixPath(value)) != value:
        raise BackupError('备份路径未规范化。')
    return value


def _name_key(value):
    return unicodedata.normalize('NFC', value).casefold()


def _reject_link(path, info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise BackupError('数据目录含符号链接或重解析点，未读取链接目标。')
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise BackupError('数据目录含硬链接；请先将文件复制为独立文件。')


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root):
    files, directories, keys = [], [], set()
    total = 0
    def visit(directory):
        nonlocal total
        for child in sorted(directory.iterdir()):
            rel = child.relative_to(root).as_posix()
            info = child.lstat()
            _reject_link(child, info)
            if rel in _LOCK_DIRS:
                if not stat.S_ISDIR(info.st_mode):
                    raise BackupError('事务锁目录形态异常。')
                continue
            if child.name.startswith(('.create-', '.tmp-')):
                raise BackupError('存在进行中或未清理的事务暂存文件，请停止写入并处理后再备份。')
            _relative(rel)
            key = _name_key(rel)
            if key in keys:
                raise BackupError('文件名在目标平台上发生大小写或 Unicode 冲突。')
            keys.add(key)
            if stat.S_ISDIR(info.st_mode):
                directories.append(rel)
                visit(child)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_BYTES or len(files) >= MAX_FILES:
                    raise BackupError('数据量超过本工具的安全备份上限。')
                files.append({'path': rel, 'size': info.st_size, 'sha256': _file_hash(child)})
                current = child.lstat()
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                    raise BackupError('备份期间文件发生变化，请停止写入后重试。')
            else:
                raise BackupError('数据目录含非常规文件，未读取。')
    visit(root)
    return {'files': files, 'directories': directories}


def _source_root(data_root):
    raw = Path(data_root).expanduser().absolute()
    if not raw.exists() or not raw.is_dir():
        raise BackupError('数据目录不存在。')
    _reject_link(raw, raw.lstat())
    return raw.resolve()


def _client_paths(root):
    clients = root / 'clients'
    if not clients.exists():
        return []
    _reject_link(clients, clients.lstat())
    return sorted(p for p in clients.iterdir() if p.is_dir() and not p.name.startswith('.'))


def create_backup(data_root, archive_path):
    root = _source_root(data_root)
    destination = Path(archive_path).expanduser().absolute()
    if root == destination.resolve() or root in destination.resolve().parents:
        raise BackupError('备份文件必须放在数据目录之外。')
    if os.path.lexists(destination):
        raise BackupError('备份目标已存在；不会覆盖既有文件。')
    if not destination.parent.exists():
        private_fs.secure_dir(destination.parent)
    client_paths = _client_paths(root)
    with ExitStack() as stack:
        # Requests may commit into authoring drafts or client/public repositories.
        # Match their outer request lock before any destination lock, so backups
        # cannot observe half a submission or deadlock against request adoption.
        # The final inventory also detects new clients.
        for path in [root / 'authoring-requests', root / 'public-proposals', root / 'tasks', root / 'authoring-workspaces', *client_paths]:
            _reject_link(path, path.lstat()) if os.path.lexists(path) else None
            stack.enter_context(profile_lock(path))
        before = _inventory(root)
        if client_paths != _client_paths(root):
            raise BackupError('客户集合正在变化，请停止写入后重试。')
        manifest = {'format': FORMAT, 'schema_version': 1,
                    'created_at': datetime.now(timezone.utc).isoformat(), **before}
        created = False
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, 'wb') as output:
                private_fs.secure_path(destination)
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8'))
                    for item in before['files']:
                        digest = hashlib.sha256()
                        with (root / item['path']).open('rb') as source, archive.open('data/' + item['path'], 'w') as target:
                            while chunk := source.read(CHUNK):
                                digest.update(chunk)
                                target.write(chunk)
                        if digest.hexdigest() != item['sha256']:
                            raise BackupError('备份期间文件内容发生变化，未生成可用备份。')
                output.flush()
                os.fsync(output.fileno())
            if before != _inventory(root) or client_paths != _client_paths(root):
                raise BackupError('备份期间目录发生变化，未生成可用备份。')
            checked = check_backup(destination)
            return {'archive_path': str(destination), 'file_count': checked['file_count'],
                    'total_bytes': checked['total_bytes'], 'verified': True,
                    'excluded': ['.locks/', 'clients/.locks/']}
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise


def _manifest(archive):
    infos = archive.infolist()
    if len(infos) > MAX_FILES + 1:
        raise BackupError('备份文件数量超过安全上限。')
    seen = set()
    total = 0
    for info in infos:
        key = _name_key(info.filename)
        if key in seen:
            raise BackupError('备份包包含重复路径。')
        seen.add(key)
        _relative(info.filename)
        mode = info.external_attr >> 16
        if (stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))
                or info.is_dir() or info.flag_bits & 1):
            raise BackupError('备份包包含链接、非常规条目或不支持的加密内容。')
        if info.filename != MANIFEST and not info.filename.startswith('data/'):
            raise BackupError('备份包包含清单外的文件。')
        total += info.file_size
        if total > MAX_BYTES or info.file_size < 0:
            raise BackupError('备份解压后数据量超过安全上限。')
    try:
        entry = archive.getinfo(MANIFEST)
        if entry.file_size > 64 * 1024 * 1024:
            raise BackupError('备份清单过大。')
        manifest = json.loads(archive.read(entry).decode('utf-8'))
    except (KeyError, ValueError, UnicodeError) as exc:
        raise BackupError('备份清单缺失或损坏。') from exc
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('schema_version') != 1:
        raise BackupError('备份格式或版本不受支持。')
    files, directories = manifest.get('files'), manifest.get('directories')
    if not isinstance(files, list) or not isinstance(directories, list):
        raise BackupError('备份清单结构不完整。')
    if len(files) + len(directories) > MAX_FILES:
        raise BackupError('备份文件和目录总数超过安全上限。')
    paths, names = set(), set()
    for item in files:
        if (not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}
                or type(item['size']) is not int or item['size'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
            raise BackupError('文件清单结构或校验值无效。')
        path = _relative(item['path'])
        if _name_key(path) in names:
            raise BackupError('备份清单含重复文件路径。')
        names.add(_name_key(path)); paths.add(path)
        try:
            info = archive.getinfo('data/' + path)
        except KeyError as exc:
            raise BackupError('备份缺少清单中声明的文件。') from exc
        if info.file_size != item['size']:
            raise BackupError('文件大小与清单不一致。')
    for directory in directories:
        directory = _relative(directory)
        if _name_key(directory) in names:
            raise BackupError('备份清单的目录与文件路径冲突。')
        names.add(_name_key(directory))
    all_paths = paths | set(directories)
    for path in all_paths:
        if path in _LOCK_DIRS or any(p.as_posix() in _LOCK_DIRS for p in PurePosixPath(path).parents):
            raise BackupError('备份不得携带运行中的事务锁。')
        for parent in PurePosixPath(path).parents:
            if parent.as_posix() != '.' and parent.as_posix() not in directories:
                raise BackupError('备份清单缺少父目录或存在文件目录冲突。')
    if set(archive.namelist()) != {MANIFEST, *('data/' + p for p in paths)}:
        raise BackupError('备份包文件与清单不一致。')
    return manifest


def _verify_contents(archive, manifest, destination=None):
    for item in manifest['files']:
        digest = hashlib.sha256()
        size = 0
        with ExitStack() as stack:
            source = stack.enter_context(archive.open('data/' + item['path']))
            target = None
            if destination is not None:
                path = destination / item['path']
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                target = stack.enter_context(os.fdopen(fd, 'wb'))
                private_fs.secure_path(path)
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > item['size']:
                    raise BackupError('解压内容超出声明大小。')
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            if target is not None:
                target.flush(); os.fsync(target.fileno())
        if size != item['size'] or digest.hexdigest() != item['sha256']:
            raise BackupError('备份文件 SHA-256 校验失败。')


def check_backup(archive_path):
    """Read-only validation: never instantiate a repository or recover transactions."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _manifest(archive)
            _verify_contents(archive, manifest)
            return {'format': FORMAT, 'schema_version': 1, 'verified': True,
                    'file_count': len(manifest['files']),
                    'total_bytes': sum(f['size'] for f in manifest['files'])}
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise BackupError('备份压缩包损坏或使用了不支持的格式。') from exc


def restore_backup(archive_path, destination):
    target = Path(destination).expanduser().absolute()
    if os.path.lexists(target):
        raise BackupError('恢复目标必须是尚不存在的新目录；不会覆盖现有客户资料。')
    check_backup(archive_path)  # Validate the entire archive before creating any destination.
    if not target.parent.is_dir():
        raise BackupError('恢复目标的上级目录必须已存在。')
    target.mkdir(mode=0o700, exist_ok=False)
    identity = target.stat()
    try:
        private_fs.secure_path(target)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = _manifest(archive)
            for directory in sorted(manifest['directories'], key=lambda p: (len(PurePosixPath(p).parts), p)):
                private_fs.secure_dir(target / directory)
            _verify_contents(archive, manifest, target)
        # Rules remain pinned to the public fingerprint stored in the original files.
        return {'destination': str(target.resolve()), 'restored': True,
                'file_count': len(manifest['files']), 'public_version_rebound': False}
    except Exception as exc:
        current = target.lstat() if target.exists() else None
        if current and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            shutil.rmtree(target)
        if isinstance(exc, (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError)):
            raise BackupError('恢复期间备份包发生变化或损坏，已移除未完成的恢复目录。') from exc
        raise
