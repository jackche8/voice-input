"""操作系统平台检测与跨平台适配器工厂。"""
from __future__ import annotations

import sys

from _platform.protocol import OsName, PlatformAdapter


def detect_os(sys_platform: str | None = None) -> OsName:
    """将 `sys.platform` 映射为规范三端标识：`linux` / `windows` / `macos`。"""
    plat = (sys.platform if sys_platform is None else sys_platform).strip().lower()
    if plat.startswith("linux"):
        return "linux"
    if plat in {"win32", "cygwin", "msys"} or plat.startswith("win"):
        return "windows"
    if plat == "darwin" or plat.startswith("darwin"):
        return "macos"
    raise RuntimeError(f"不支持的操作系统平台: {sys_platform or sys.platform!r}")


def create_platform_adapter(*, os_name: OsName | None = None, **kwargs) -> PlatformAdapter:
    """按平台按需延迟导入并构造对应 `PlatformAdapter` 实例。"""
    target = detect_os() if os_name is None else os_name
    if target == "linux":
        from _platform.linux import LinuxPlatformAdapter

        return LinuxPlatformAdapter(**kwargs)
    if target == "windows":
        from _platform.windows import WindowsPlatformAdapter

        return WindowsPlatformAdapter(**kwargs)
    if target == "macos":
        from _platform.macos import MacOSPlatformAdapter

        return MacOSPlatformAdapter(**kwargs)
    raise ValueError(f"未知平台类型: {target!r}")
