"""统一跨平台适配层（`_platform`）：Linux / Windows / macOS 桌面能力与本地 Root CA 证书管理。

只提供原子系统适配能力（Logic 层），不含业务流程或 UI 组件。
"""
from __future__ import annotations

import importlib
from typing import Any

from _platform.detect import create_platform_adapter, detect_os
from _platform.protocol import (
    FocusTarget,
    HotkeyCallbacks,
    HotkeyRig,
    OsName,
    PasteResult,
    PlatformAdapter,
    ScreenCapture,
    VirtualRect,
    capture_region_mss,
    crop_png_region,
)

# PEP 562 延迟导出 `_platform.certs`，防止 `python3 -m _platform.certs` 触发 runpy 预导入告警
_CERT_EXPORTS = frozenset(
    {
        "APPLE_MAX_LEAF_DAYS",
        "DEFAULT_SERVER_CERT_DAYS",
        "CertificateInspection",
        "discover_lan_ipv4s",
        "ensure_tls_certificates",
        "inspect_certificate",
    }
)


def __getattr__(name: str) -> Any:
    if name in _CERT_EXPORTS:
        certs = importlib.import_module("_platform.certs")
        return getattr(certs, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "APPLE_MAX_LEAF_DAYS",
    "DEFAULT_SERVER_CERT_DAYS",
    "CertificateInspection",
    "FocusTarget",
    "HotkeyCallbacks",
    "HotkeyRig",
    "OsName",
    "PasteResult",
    "PlatformAdapter",
    "ScreenCapture",
    "VirtualRect",
    "capture_region_mss",
    "create_platform_adapter",
    "crop_png_region",
    "detect_os",
    "discover_lan_ipv4s",
    "ensure_tls_certificates",
    "inspect_certificate",
]
