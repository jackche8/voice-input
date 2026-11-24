"""Windows Win32 `user32`/`kernel32` 底层系统接口与 64 位安全 `ctypes` 签名绑定。

所有 64 位句柄/指针返回类型（`HWND`/`HANDLE`/`HGLOBAL`/`c_void_p`/`c_size_t`）均在
`_configure_win32_signatures()` 中显式声明，避免 64 位 Windows 上默认 `c_int` 截断高 32 位地址导致访问违规崩溃。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Literal, Protocol

from _hotkey import KEY_TABLE, WIN32_MODIFIERS

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = KEY_TABLE["v"].vk

_WIN32_ASYNC_MOD_KEYS = (
    (VK_CONTROL, WIN32_MODIFIERS["ctrl"]),
    (VK_MENU, WIN32_MODIFIERS["alt"]),
    (VK_SHIFT, WIN32_MODIFIERS["shift"]),
    (VK_LWIN, WIN32_MODIFIERS["super"]),
    (VK_RWIN, WIN32_MODIFIERS["super"]),
)

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1

CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class WinClipboardSnapshot:
    kind: Literal["empty", "text", "dib", "unsupported", "failed"]
    text: str = ""
    dib_bytes: bytes = b""


class Win32Api(Protocol):
    def get_current_pid(self) -> int: ...

    def get_foreground_window(self) -> int: ...

    def get_window_pid(self, hwnd: int) -> int: ...

    def get_window_title(self, hwnd: int) -> str: ...

    def get_process_image_name(self, pid: int) -> str: ...

    def send_unicode_text(self, text: str) -> bool: ...

    def send_vk_combo(self, vk_codes: Sequence[int]) -> bool: ...

    def read_clipboard_text(self) -> str: ...

    def write_clipboard_text(self, text: str) -> str | None: ...

    def write_clipboard_dib(self, dib: bytes) -> str | None: ...

    def snapshot_clipboard(self) -> WinClipboardSnapshot: ...

    def restore_clipboard(self, snap: WinClipboardSnapshot) -> None: ...


def _configure_win32_signatures(user32, kernel32) -> None:
    """为所有调用到的 Win32 `user32` / `kernel32` 函数显式声明 64 位安全 `argtypes` 与 `restype`。"""
    user32_sigs = {
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetWindowThreadProcessId": ([wintypes.HWND, wintypes.LPDWORD], wintypes.DWORD),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "SendInput": ([wintypes.UINT, ctypes.c_void_p, ctypes.c_int], wintypes.UINT),
        "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
        "OpenClipboard": ([wintypes.HWND], wintypes.BOOL),
        "CloseClipboard": ([], wintypes.BOOL),
        "EmptyClipboard": ([], wintypes.BOOL),
        "EnumClipboardFormats": ([wintypes.UINT], wintypes.UINT),
        "IsClipboardFormatAvailable": ([wintypes.UINT], wintypes.BOOL),
        "GetClipboardData": ([wintypes.UINT], wintypes.HANDLE),
        "SetClipboardData": ([wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
        "RegisterClipboardFormatW": ([wintypes.LPCWSTR], wintypes.UINT),
    }
    kernel32_sigs = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "QueryFullProcessImageNameW": (
            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD],
            wintypes.BOOL,
        ),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GlobalAlloc": ([wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL),
        "GlobalLock": ([wintypes.HGLOBAL], ctypes.c_void_p),
        "GlobalUnlock": ([wintypes.HGLOBAL], wintypes.BOOL),
        "GlobalSize": ([wintypes.HGLOBAL], ctypes.c_size_t),
        "GlobalFree": ([wintypes.HGLOBAL], wintypes.HGLOBAL),
    }
    for name, (argtypes, restype) in user32_sigs.items():
        fn = getattr(user32, name)
        fn.argtypes = argtypes
        fn.restype = restype
    for name, (argtypes, restype) in kernel32_sigs.items():
        fn = getattr(kernel32, name)
        fn.argtypes = argtypes
        fn.restype = restype


class CtypesWin32Api:
    """真机 Windows `user32` / `kernel32` 实现（仅在 Windows 调用或显式注入 DLL 时绑定）。"""

    def __init__(self, *, user32=None, kernel32=None) -> None:
        if user32 is None or kernel32 is None:
            windll = getattr(ctypes, "windll", None)
            if windll is None:
                raise RuntimeError("当前系统不支持 ctypes.windll（非 Windows 平台）")
            user32 = windll.user32 if user32 is None else user32
            kernel32 = windll.kernel32 if kernel32 is None else kernel32
        self._user32 = user32
        self._kernel32 = kernel32
        _configure_win32_signatures(self._user32, self._kernel32)
        self._clip_lock = threading.Lock()
        self._build_input_structs()

    def _build_input_structs(self) -> None:
        ulong_ptr = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", wintypes.DWORD),
                ("u", INPUT_UNION),
            ]

        self._KEYBDINPUT = KEYBDINPUT
        self._INPUT_UNION = INPUT_UNION
        self._INPUT = INPUT

    def get_current_pid(self) -> int:
        return os.getpid()

    def get_foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def get_window_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)

    def get_window_title(self, hwnd: int) -> str:
        length = int(self._user32.GetWindowTextLengthW(wintypes.HWND(hwnd)) or 0)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
        return buf.value

    def get_process_image_name(self, pid: int) -> str:
        if pid <= 0:
            return ""
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
        )
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            ok = self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            self._kernel32.CloseHandle(handle)

    def _send_inputs(self, events: list) -> bool:
        if not events:
            return True
        arr = (self._INPUT * len(events))(*events)
        sent = int(self._user32.SendInput(len(events), ctypes.byref(arr), ctypes.sizeof(self._INPUT)) or 0)
        return sent == len(events)

    def send_unicode_text(self, text: str) -> bool:
        if not text:
            return True
        utf16 = text.encode("utf-16-le")
        units = [int.from_bytes(utf16[i : i + 2], "little") for i in range(0, len(utf16), 2)]
        events = []
        for code_unit in units:
            events.append(
                self._INPUT(
                    type=INPUT_KEYBOARD,
                    u=self._INPUT_UNION(
                        ki=self._KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
                    ),
                )
            )
            events.append(
                self._INPUT(
                    type=INPUT_KEYBOARD,
                    u=self._INPUT_UNION(
                        ki=self._KEYBDINPUT(
                            wVk=0,
                            wScan=code_unit,
                            dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
            )
        return self._send_inputs(events)

    def send_vk_combo(self, vk_codes: Sequence[int]) -> bool:
        events = []
        for vk in vk_codes:
            events.append(
                self._INPUT(
                    type=INPUT_KEYBOARD,
                    u=self._INPUT_UNION(ki=self._KEYBDINPUT(wVk=int(vk), wScan=0, dwFlags=0, time=0, dwExtraInfo=0)),
                )
            )
        for vk in reversed(vk_codes):
            events.append(
                self._INPUT(
                    type=INPUT_KEYBOARD,
                    u=self._INPUT_UNION(
                        ki=self._KEYBDINPUT(wVk=int(vk), wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
                    ),
                )
            )
        return self._send_inputs(events)

    def _open_clipboard_retry(self) -> bool:
        for _ in range(10):
            if self._user32.OpenClipboard(None):
                return True
            time.sleep(0.01)
        return False

    def read_clipboard_text(self) -> str:
        with self._clip_lock:
            if not self._open_clipboard_retry():
                return ""
            try:
                if not self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                handle = self._user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = self._kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    self._kernel32.GlobalUnlock(handle)
            finally:
                self._user32.CloseClipboard()

    def write_clipboard_text(self, text: str) -> str | None:
        encoded = (text + "\x00").encode("utf-16-le")
        return self._write_clipboard_bytes(CF_UNICODETEXT, encoded, label="剪贴板文本")

    def write_clipboard_dib(self, dib: bytes) -> str | None:
        return self._write_clipboard_bytes(CF_DIB, dib, label="剪贴板位图")

    def _write_clipboard_bytes(self, fmt: int, payload: bytes, *, label: str) -> str | None:
        with self._clip_lock:
            if not self._open_clipboard_retry():
                return "无法打开 Windows 剪贴板"
            try:
                self._user32.EmptyClipboard()
                hmem = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
                if not hmem:
                    return f"GlobalAlloc 分配{label}内存失败"
                ptr = self._kernel32.GlobalLock(hmem)
                if not ptr:
                    self._kernel32.GlobalFree(hmem)
                    return f"GlobalLock 锁定{label}内存失败"
                ctypes.memmove(ptr, payload, len(payload))
                self._kernel32.GlobalUnlock(hmem)
                if not self._user32.SetClipboardData(fmt, hmem):
                    self._kernel32.GlobalFree(hmem)
                    return f"SetClipboardData 写入{label}失败"
                return None
            finally:
                self._user32.CloseClipboard()

    def snapshot_clipboard(self) -> WinClipboardSnapshot:
        with self._clip_lock:
            if not self._open_clipboard_retry():
                return WinClipboardSnapshot(kind="failed")
            try:
                formats: list[int] = []
                fmt = int(self._user32.EnumClipboardFormats(0) or 0)
                while fmt:
                    formats.append(fmt)
                    fmt = int(self._user32.EnumClipboardFormats(fmt) or 0)

                if not formats:
                    return WinClipboardSnapshot(kind="empty")
                if CF_HDROP in formats:
                    return WinClipboardSnapshot(kind="unsupported")

                has_text = bool(self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
                has_dib = bool(self._user32.IsClipboardFormatAvailable(CF_DIB))
                if has_dib and not has_text:
                    handle = self._user32.GetClipboardData(CF_DIB)
                    if not handle:
                        return WinClipboardSnapshot(kind="failed")
                    size = int(self._kernel32.GlobalSize(handle) or 0)
                    if size <= 0:
                        return WinClipboardSnapshot(kind="failed")
                    ptr = self._kernel32.GlobalLock(handle)
                    if not ptr:
                        return WinClipboardSnapshot(kind="failed")
                    try:
                        return WinClipboardSnapshot(kind="dib", dib_bytes=ctypes.string_at(ptr, size))
                    finally:
                        self._kernel32.GlobalUnlock(handle)

                if has_text:
                    handle = self._user32.GetClipboardData(CF_UNICODETEXT)
                    if not handle:
                        return WinClipboardSnapshot(kind="failed")
                    ptr = self._kernel32.GlobalLock(handle)
                    if not ptr:
                        return WinClipboardSnapshot(kind="failed")
                    try:
                        return WinClipboardSnapshot(kind="text", text=ctypes.wstring_at(ptr))
                    finally:
                        self._kernel32.GlobalUnlock(handle)

                return WinClipboardSnapshot(kind="unsupported")
            finally:
                self._user32.CloseClipboard()

    def restore_clipboard(self, snap: WinClipboardSnapshot) -> None:
        if snap.kind in ("unsupported", "failed"):
            return
        if snap.kind == "text":
            self.write_clipboard_text(snap.text)
            return
        if snap.kind == "dib":
            if snap.dib_bytes:
                self.write_clipboard_dib(snap.dib_bytes)
            return
        if snap.kind == "empty":
            with self._clip_lock:
                if not self._open_clipboard_retry():
                    return
                try:
                    self._user32.EmptyClipboard()
                finally:
                    self._user32.CloseClipboard()


def set_windows_app_user_model_id(app_id: str, *, shell32=None) -> bool:
    """为当前 Windows 进程注册显式 AppUserModelID（任务栏分组与图标归属单源）。非 Windows 下为无操作返回 False。"""
    if not isinstance(app_id, str) or not app_id.strip():
        raise ValueError(f"AppUserModelID 不能为空，收到: {app_id!r}")
    if shell32 is None:
        if os.name != "nt":
            return False
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        shell32 = windll.shell32
    fn = getattr(shell32, "SetCurrentProcessExplicitAppUserModelID", None)
    if fn is None:
        return False
    fn.argtypes = [wintypes.LPCWSTR]
    fn.restype = ctypes.c_long
    hr = int(fn(app_id.strip()))
    return hr == 0


def read_win32_modifier_mask(*, user32=None) -> int | None:
    """通过 Win32 `GetAsyncKeyState` 读取当前真实物理修饰键掩码（防 UAC / Ctrl+Alt+Del 丢 KEYUP 粘滞）。

    内部自行显式声明 `argtypes`/`restype`，不依赖 `CtypesWin32Api.__init__` 先执行；
    非 Windows 且未注入 `user32` 时返回 `None`。
    """
    if user32 is None:
        if os.name != "nt":
            return None
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        user32 = windll.user32
    fn = getattr(user32, "GetAsyncKeyState", None)
    if fn is None:
        return None
    fn.argtypes = [ctypes.c_int]
    fn.restype = ctypes.c_short
    mask = 0
    for vk, flag in _WIN32_ASYNC_MOD_KEYS:
        if int(fn(vk)) & 0x8000:
            mask |= flag
    return mask
