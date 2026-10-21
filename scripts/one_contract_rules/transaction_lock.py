"""Per-profile reentrant thread/process transaction lock (local filesystems)."""
from __future__ import annotations
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from . import private_fs

class TransactionBusyError(OSError):
    pass

_guard = threading.Lock()
_locks = {}
_local = threading.local()

@contextmanager
def profile_lock(profile_dir: Path, *, timeout: float = 15.0):
    profile_dir = Path(profile_dir).resolve()
    key = (os.getpid(), str(profile_dir))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise TransactionBusyError("客户数据正在被另一操作使用，请稍后重试。")
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    handle = None
    try:
        if key in held:
            yield
            return
        lock_dir = profile_dir.parent / ".locks"
        private_fs.secure_dir(lock_dir)
        lock_path = lock_dir / (profile_dir.name + ".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        private_fs.secure_path(lock_path)
        if os.fstat(fd).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TransactionBusyError("客户数据事务锁等待超时，请稍后重试。")
                time.sleep(0.025)
            except OSError as exc:
                if os.name != "nt" or exc.errno not in (11, 13, 36):
                    raise
                if time.monotonic() >= deadline:
                    raise TransactionBusyError("客户数据事务锁等待超时。") from exc
                time.sleep(0.025)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        lock.release()
