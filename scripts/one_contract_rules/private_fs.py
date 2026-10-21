#!/usr/bin/env python3
"""私密目录与文件的权限收口。

契约来源：safety-and-acceptance.md SEC-013（客户目录 0700、文件 0600）；
architecture-contract.md §8.2（显示名等属私有数据，不进入公开包或普通日志）；
§8.5 / §10（快照与审计的落盘）。

**为什么单独成模块**：权限不能靠"每个写入点记得写 chmod"。
`client_repository` 与 `proposal_repository` 各自做了一份收口，而
`publisher`（active-manifest.json、snapshots/*/manifest.json、rules.json）
与 `audit`（audit/events.jsonl）各有自己的写入路径、都没有收口，
于是这些文件以 0644 落盘、目录以 0755 落盘——客户规则正文与审计记录
同机其他用户可读（W7-SEC013 的成因）。把收口收敛到一处，新写入点
只要走这里的函数就不会再漏。

模式常量与 `client_repository` / `proposal_repository` 保持一致；
本模块只负责"权限与原子落盘"，不承担任何业务语义。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping
from . import platform_fs

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "atomic_write_json",
    "secure_append_line",
    "secure_dir",
    "secure_path",
]

#: 私密目录/文件权限：同机其他用户不可读、不可进入
DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_dir(path: Path) -> None:
    path = Path(path)
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(str(path))
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
        platform_fs.secure_path(directory, directory=True)
    platform_fs.secure_path(path, directory=True)


def secure_path(path: Path) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        platform_fs.secure_path(path, directory=path.is_dir())


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    secure_dir(path.parent)
    if path.is_symlink():
        raise OSError("私有文件不能是符号链接。")
    tmp = path.parent / f".tmp-{path.name}.{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            secure_path(tmp)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        platform_fs.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def secure_append_line(path: Path, line: str) -> None:
    # Caller serializes the read/append/replace transaction. Atomic replacement
    # leaves either a complete old log or a complete new log after interruption.
    path = Path(path)
    previous = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write_text(path, previous + line)
