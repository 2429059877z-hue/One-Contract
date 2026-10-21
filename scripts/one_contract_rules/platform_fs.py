"""OS-specific private storage. Windows uses protected DACLs, not chmod bits.

Windows replacement uses MoveFileExW(WRITE_THROUGH); it does not emulate POSIX
fsync(directory), which the Windows CRT cannot open. File contents are fsynced
before replacement. Power-loss guarantees still depend on the underlying volume.
"""
from __future__ import annotations
import os
from pathlib import Path


def secure_path(path: Path, *, directory: bool) -> None:
    if Path(path).is_symlink():
        raise OSError("私有存储不能通过符号链接设置权限。")
    if os.name == "nt":
        _windows_private_acl(Path(path), directory=directory)
    else:
        os.chmod(path, 0o700 if directory else 0o600)


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        # Directory CRT handles are unsupported. All our replacements use the
        # explicit Windows write-through API below, rather than swallowing EACCES.
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # Directory publication never replaces an existing directory.
        flags = 0x8 | (0 if Path(source).is_dir() else 0x1)
        if not move(str(source), str(destination), flags):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, destination)
    sync_directory(Path(destination).parent)


def _windows_private_acl(path: Path, *, directory: bool) -> None:
    """Protected DACL: current token user and LocalSystem only. Fail closed.

    SetNamedSecurityInfoW rejects unsupported filesystems/insufficient WRITE_DAC;
    errors must reach the caller before any private payload is written.
    """
    import ctypes
    from ctypes import wintypes as w
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.c_void_p
    kernel.GetVolumePathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel.GetVolumePathNameW.restype = w.BOOL
    kernel.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
    kernel.GetVolumeInformationW.restype = w.BOOL
    volume = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(path.resolve()), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    flags = w.DWORD()
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not flags.value & 0x8:  # FILE_PERSISTENT_ACLS
        raise OSError("客户数据目录所在卷不支持持久访问权限；请选择支持 ACL 的本地目录。")
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.OpenProcessToken.restype = w.BOOL
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ptr, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.GetTokenInformation.restype = w.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = w.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ptr), ctypes.POINTER(w.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    adv.GetSecurityDescriptorDacl.argtypes = [ptr, ctypes.POINTER(w.BOOL), ctypes.POINTER(ptr), ctypes.POINTER(w.BOOL)]
    adv.GetSecurityDescriptorDacl.restype = w.BOOL
    adv.SetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, ptr, ptr, ptr, ptr]
    adv.SetNamedSecurityInfoW.restype = w.DWORD
    token = w.HANDLE()
    sid_string = w.LPWSTR()
    sd = ptr()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER begins with SID_AND_ATTRIBUTES; its first member is PSID.
        sid = ctypes.cast(buf, ctypes.POINTER(ptr))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        inherit = "OICI" if directory else ""
        sddl = f"D:P(A;{inherit};FA;;;{sid_string.value})(A;{inherit};FA;;;SY)"
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise ctypes.WinError(ctypes.get_last_error())
        present, defaulted, dacl = w.BOOL(), w.BOOL(), ptr()
        if not adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present or not dacl:
            raise OSError("无法创建 Windows 私有 DACL。")
        status = adv.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000, None, None, dacl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        if sd:
            kernel.LocalFree(sd)
        if sid_string:
            kernel.LocalFree(ctypes.cast(sid_string, ptr))
        if token:
            kernel.CloseHandle(token)
