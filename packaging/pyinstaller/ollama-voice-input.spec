# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单可执行 --onedir 打包规格（Windows x64 / macOS arm64 共用）。

- 单入口 `packaging/pyinstaller/entry_daemon.py`，4 路子进程（child_guard / server / setup_worker / window）
  全部经 `--internal-*` 派发为独立 OS 进程，零 Qt 预导入；
- `--onedir` + `upx=False` 保持 PySide6/Qt、PyAV/FFmpeg 等 LGPL 动态库在 `_internal/` 内可独立替换；
- `runtimes/asr` 及外部 ASR 解释器的 4 个本地导入闭包（`_ports.py`、`_contracts.py`、`_audio_codec.py`、`_procfs.py`）
  唯一隔离在 `_asr_runtime/`，避免 `_MEIPASS` 根的 `_internal/` 遮蔽 ASR 独立 venv 的二进制依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _version import APP_VERSION  # noqa: E402

datas: list[tuple[str, str]] = [
    (str(ROOT / "static" / "v3"), "static/v3"),
    (str(ROOT / "_v3" / "asr_download_worker.py"), "_v3"),
    # 外部 ASR 解释器（paths.asr_python -m runtimes.asr）唯一隔离运行目录（不放 _MEIPASS 根）
    (str(ROOT / "runtimes" / "asr"), "_asr_runtime/runtimes/asr"),
    (str(ROOT / "_ports.py"), "_asr_runtime"),
    (str(ROOT / "_contracts.py"), "_asr_runtime"),
    (str(ROOT / "_audio_codec.py"), "_asr_runtime"),
    (str(ROOT / "_procfs.py"), "_asr_runtime"),
    # 运行时图标资源与双许可/第三方合规文档（严禁将 .svg 传给 EXE/BUNDLE icon=）
    (str(ROOT / "resources" / "ollama-voice-input.svg"), "resources"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "COMMERCIAL.md"), "."),
    (str(ROOT / "THIRD-PARTY-NOTICES.md"), "."),
]
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = [
    "app",
    "_v3.setup_worker",
    "_platform.child_guard",
    "_platform.certs",
    "_platform._certs_crypto",
    "_platform._certs_openssl",
    "_window.__main__",
    "_window.pywebview_host",
    "python_multipart",
    "PySide6.QtDBus",
]

for _pkg in ("webview", "uvicorn", "av", "cryptography"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

if sys.platform in ("win32", "darwin"):
    datas += collect_data_files("_sounddevice_data")

if sys.platform == "win32":
    for _win_pkg in ("pythonnet", "clr_loader"):
        _d, _b, _h = collect_all(_win_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    hiddenimports += [
        "_platform.windows",
        "_platform._win32_api",
        "_platform._child_windows",
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
    ]
elif sys.platform == "darwin":
    # webview 已在上方经 collect_all("webview") 收集全部包资源；显式补充 Cocoa 叶子后端避免静态漏收
    hiddenimports += [
        "webview.platforms.cocoa",
        "_platform.macos",
        "_platform._macos_api",
        "_platform._child_posix",
        "pynput.keyboard._darwin",
        "pynput.mouse._darwin",
    ]
else:
    hiddenimports += [
        "_platform.linux",
        "_platform._child_posix",
        "_window.webkit_gtk",
        "pynput.keyboard._xorg",
        "pynput.mouse._xorg",
    ]

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entry_daemon.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 切勿排除 _linux：_v3/production.py 顶层导入纯跨平台的 _linux.conflicts / _linux.evdev_keyboards
    excludes=["tkinter", "pytest", "tests", *(["gi", "dbus", "evdev", "Xlib"] if sys.platform != "linux" else [])],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ollama-voice-input",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ollama-voice-input",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Ollama Voice Input.app",
        icon=None,
        bundle_identifier="io.github.jackche8.ollama-voice-input",
        info_plist={
            "CFBundleName": "Ollama Voice Input",
            "CFBundleDisplayName": "Ollama Voice Input",
            "CFBundleIdentifier": "io.github.jackche8.ollama-voice-input",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": (
                "Ollama Voice Input needs microphone access for offline push-to-talk speech recognition."
            ),
            "NSScreenCaptureUsageDescription": (
                "Ollama Voice Input needs screen recording access for screenshot translation, OCR, and annotation."
            ),
            "NSAppleEventsUsageDescription": (
                "Ollama Voice Input uses Apple Events to inspect the focused window and write images to the clipboard."
            ),
        },
    )
