"""平台模块、跨平台适配层与桌面装配模块可导入（只导入，不运行 main、不建窗口、不抓键）。"""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_PY = ROOT / ".venv" / "bin" / "python3"

CROSS_PLATFORM_PURE_MODULES = (
    "_platform",
    "_platform.protocol",
    "_platform.detect",
    "_platform._common",
    "_platform._pynput_hotkeys",
    "_platform._win32_api",
    "_platform._macos_api",
    "_platform._certs_backends",
    "_platform._certs_crypto",
    "_platform._certs_openssl",
    "_platform.linux",
    "_platform.windows",
    "_platform.macos",
    "_platform.certs",
    "_linux.ibus",
    "_linux.clipboard",
    "_linux.modifiers",
    "_linux.paste_backend",
    "_linux.hotkey_actions",
    "_linux.hotkey_state",
    "_linux.hotkey_dispatch",
    "_linux.evdev_keyboards",
    "_linux.hotkey_portal",
    "_linux.hotkey_evdev",
    "_linux.hotkey_x11",
    "_linux.hotkey_xinput",
    "_linux.hotkey_manager",
    "_linux.conflicts",
    "_linux.startup_check",
    "_linux.mic",
    "_desktop.actions",
    "_desktop.api",
    "_desktop.capture_flow",
    "_desktop.delivery",
    "_desktop.event_stream",
    "_desktop.singleton",
    "_desktop.status",
    "_desktop.voice",
)

MODULES = (
    "_platform", "_platform.protocol", "_platform.detect", "_platform._common",
    "_platform._pynput_hotkeys", "_platform._win32_api", "_platform._macos_api",
    "_platform._certs_backends", "_platform._certs_crypto", "_platform._certs_openssl",
    "_platform.linux", "_platform.windows", "_platform.macos", "_platform.certs",
    "_linux.inject_guard", "_linux.x11keys", "_linux.sd_notify", "_linux.uinput_device", "_linux.modifiers",
    "_linux.clipboard", "_linux.ibus", "_linux.paste_backend", "_linux.conflicts",
    "_linux.portal_dbus", "_linux.geometry",
    "_linux.hotkey_actions", "_linux.hotkey_state", "_linux.hotkey_dispatch", "_linux.evdev_keyboards", "_linux.hotkey_x11",
    "_linux.hotkey_xinput", "_linux.hotkey_evdev", "_linux.hotkey_portal", "_linux.hotkey_manager",
    "_linux.mic", "_linux.startup_check",
    "_desktop.actions", "_desktop.api", "_desktop.capture_flow", "_desktop.delivery",
    "_desktop.event_stream", "_desktop.singleton", "_desktop.status", "_desktop.voice",
    "daemon", "app",
)

_CHILD = """
import importlib, sys
mods = sys.argv[1:]
for m in mods:
    importlib.import_module(m)
import daemon, app
assert callable(daemon.main) and callable(app.create_production_app)
assert not hasattr(app, "app")
print("imported", len(mods))
"""


def test_cross_platform_modules_import_in_isolated_venv(monkeypatch):
    """无 GTK / 无 Xlib / 无 Win32 / 无 Quartz 的隔离环境也必须能直接导入跨平台适配层与桌面业务层。"""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in CROSS_PLATFORM_PURE_MODULES:
        importlib.import_module(name)
    daemon_mod = importlib.import_module("daemon")
    assert callable(daemon_mod.main)


def test_non_linux_platform_modules_never_import_linux():
    """Windows / macOS / 公共 / 证书模块严禁反向依赖 `_linux.*`。"""
    checked = (
        "_platform/__init__.py",
        "_platform/protocol.py",
        "_platform/_common.py",
        "_platform/_pynput_hotkeys.py",
        "_platform/_win32_api.py",
        "_platform/windows.py",
        "_platform/_macos_api.py",
        "_platform/macos.py",
        "_platform/certs.py",
        "_platform/_certs_backends.py",
        "_platform/_certs_crypto.py",
        "_platform/_certs_openssl.py",
    )
    for rel in checked:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("_linux"), f"{rel} 违规导入 {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("_linux"), f"{rel} 违规从 {mod} 导入"


def test_platform_modules_import(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        for name in MODULES:
            importlib.import_module(name)
        assert callable(importlib.import_module("daemon").main)
        return
    except ImportError:
        if not PRODUCTION_PY.exists():
            pytest.skip("隔离环境缺少可选 Linux 依赖，且没有生产解释器 .venv/bin/python3")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "NOTIFY_SOCKET")}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [str(PRODUCTION_PY), "-c", _CHILD, *MODULES],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert f"imported {len(MODULES)}" in proc.stdout
