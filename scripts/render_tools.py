"""Dependency-free tool discovery; finding tools is not visual validation."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def _windows_libreoffice_candidates():
    """Check only explicit install locations, never recurse or modify PATH."""
    locations = []
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value and Path(value).is_absolute():
            locations.append(Path(value) / "LibreOffice" / "program")
    local = os.environ.get("LOCALAPPDATA")
    if local and Path(local).is_absolute():
        locations.extend((Path(local) / "Programs/LibreOffice/program", Path(local) / "LibreOffice/program"))
    for directory in dict.fromkeys(locations):
        # The console launcher is LibreOffice's documented Windows CLI entry.
        for filename in ("soffice.com", "soffice.exe"):
            yield directory / filename


def diagnose_rendering_tools():
    """Return current tool discovery without launching processes or writing data.

    PATH has precedence. Windows gets a bounded LibreOffice installation-path
    fallback; Poppler has no assumed installation/download location. Fonts,
    DLL loading, actual conversion and page layout remain unverified.
    """
    tools = {}
    for name, aliases in (("soffice", ("soffice", "libreoffice")),
                          ("pdfinfo", ("pdfinfo",)), ("pdftoppm", ("pdftoppm",))):
        found = next((path for alias in aliases if (path := shutil.which(alias))), None)
        source = "path" if found else None
        if not found and name == "soffice" and sys.platform == "win32":
            for candidate in _windows_libreoffice_candidates():
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        found, source = str(candidate.resolve()), "windows_installation"
                        break
                except OSError:
                    continue
        tools[name] = {"path": os.path.abspath(found) if found else None, "source": source}
    missing = [name for name, tool in tools.items() if not tool["path"]]
    remediation = []
    if "soffice" in missing:
        remediation.append("请通过可信安装来源准备 LibreOffice；将其 program 目录加入启动工作台的 PATH，Windows 常规安装目录也会自动检查。")
    if "pdfinfo" in missing or "pdftoppm" in missing:
        remediation.append("请由部署负责人准备适用于本机的 Poppler，并将包含 pdfinfo、pdftoppm 的目录加入启动工作台的 PATH。")
    if missing:
        remediation.append("配置完成后重启工作台并重新检查；系统不会自动下载或安装渲染工具。")
    return {"ready": not missing, "status": "missing_tools" if missing else "tools_found",
            "tools": tools, "missing_tools": missing, "remediation": remediation,
            "font_and_layout_verified": False}
