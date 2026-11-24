"""Windows 平台适配器：Win32 `user32`/`kernel32` + `mss` 多屏抓屏 + `SendInput`/剪贴板保护 + 五键监听。

所有系统调用统一经 `_platform._win32_api.Win32Api` 协议封装：
- 真机 Windows 自动使用 `CtypesWin32Api`（`ctypes.windll.user32`/`kernel32`）+ `mss` + `pynput`；
- Linux / CI 隔离测试环境通过注入假 `win32_api`、`mss_factory` 与 `listener_factory` 完成 100% 分支契约验证。
"""
from __future__ import annotations

import io
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from _contracts import SetupCapability
from _hotkey import (
    KEY_TABLE,
    WIN32_MODIFIERS,
    HotkeyBindings,
    canonical_key,
    parse_hotkey_win32,
)
from _platform._common import (
    always_false,
    check_focus_and_self_guard,
    noop,
    play_beep as common_play_beep,
    probe_ocr_capabilities,
)
from _platform._pynput_hotkeys import PynputHotkeyRig
from _platform._win32_api import (
    CF_DIB,
    CF_HDROP,
    CF_UNICODETEXT,
    VK_CONTROL,
    VK_LWIN,
    VK_MENU,
    VK_RWIN,
    VK_SHIFT,
    VK_V,
    CtypesWin32Api,
    Win32Api,
    WinClipboardSnapshot,
    _configure_win32_signatures,
    read_win32_modifier_mask,
)
from _platform.protocol import (
    FocusTarget,
    HotkeyRig,
    PasteResult,
    ScreenCapture,
    capture_screen_mss,
)

PASTE_RESTORE_DELAY_S = 0.55
RUN_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

WINDOWS_TERMINAL_EXES = frozenset(
    {
        "windowsterminal.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "mintty.exe",
    }
)

_VK_TO_WIN32_MOD = {
    0x11: WIN32_MODIFIERS["ctrl"],   # VK_CONTROL
    0xA2: WIN32_MODIFIERS["ctrl"],   # VK_LCONTROL
    0xA3: WIN32_MODIFIERS["ctrl"],   # VK_RCONTROL
    0x12: WIN32_MODIFIERS["alt"],    # VK_MENU
    0xA4: WIN32_MODIFIERS["alt"],    # VK_LMENU
    0xA5: WIN32_MODIFIERS["alt"],    # VK_RMENU
    0x10: WIN32_MODIFIERS["shift"],  # VK_SHIFT
    0xA0: WIN32_MODIFIERS["shift"],  # VK_LSHIFT
    0xA1: WIN32_MODIFIERS["shift"],  # VK_RSHIFT
    0x5B: WIN32_MODIFIERS["super"],  # VK_LWIN
    0x5C: WIN32_MODIFIERS["super"],  # VK_RWIN
}

_PYNPUT_NAME_TO_MOD = {
    "ctrl": WIN32_MODIFIERS["ctrl"],
    "ctrl_l": WIN32_MODIFIERS["ctrl"],
    "ctrl_r": WIN32_MODIFIERS["ctrl"],
    "alt": WIN32_MODIFIERS["alt"],
    "alt_l": WIN32_MODIFIERS["alt"],
    "alt_r": WIN32_MODIFIERS["alt"],
    "alt_gr": WIN32_MODIFIERS["alt"],
    "shift": WIN32_MODIFIERS["shift"],
    "shift_l": WIN32_MODIFIERS["shift"],
    "shift_r": WIN32_MODIFIERS["shift"],
    "cmd": WIN32_MODIFIERS["super"],
    "cmd_l": WIN32_MODIFIERS["super"],
    "cmd_r": WIN32_MODIFIERS["super"],
}


def _extract_win32_key_info(key) -> tuple[int, int]:
    """从 pynput key 事件提取 `(modifier_flag, vk_code)`。"""
    name = getattr(key, "name", None)
    if isinstance(name, str):
        low = name.lower()
        if low in _PYNPUT_NAME_TO_MOD:
            mod_flag = _PYNPUT_NAME_TO_MOD[low]
            val = getattr(key, "value", None)
            raw_vk = getattr(val, "vk", None) if val is not None else None
            if raw_vk is None:
                raw_vk = getattr(key, "vk", None)
            vk = int(raw_vk) if raw_vk is not None else 0
            return mod_flag, vk

    raw_vk = getattr(key, "vk", None)
    if raw_vk is None:
        val = getattr(key, "value", None)
        raw_vk = getattr(val, "vk", None) if val is not None else None
    vk = int(raw_vk) if raw_vk is not None else 0
    if vk in _VK_TO_WIN32_MOD:
        return _VK_TO_WIN32_MOD[vk], vk

    if not vk:
        ch = getattr(key, "char", None)
        token = ch if isinstance(ch, str) and ch else (name if isinstance(name, str) else "")
        if token:
            entry = KEY_TABLE.get(canonical_key(token))
            if entry is not None:
                vk = entry.vk
    return 0, vk


def _png_to_dib(png: bytes) -> bytes:
    """把 PNG 字节转为 Windows CF_DIB（24 位 RGB BMP 去掉 14 字节 BITMAPFILEHEADER）。"""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="BMP")
    raw = buf.getvalue()
    if len(raw) <= 14 or not raw.startswith(b"BM"):
        raise RuntimeError("BMP 编码结果缺少有效文件头")
    return raw[14:]


_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_PRESS_MESSAGES = (_WM_KEYDOWN, _WM_SYSKEYDOWN)
_WM_RELEASE_MESSAGES = (_WM_KEYUP, _WM_SYSKEYUP)
_VK_PACKET = 0xE7
_LLKHF_LOWER_IL_INJECTED = 0x00000002
_LLKHF_INJECTED = 0x00000010
_LLKHF_ANY_INJECTED = _LLKHF_INJECTED | _LLKHF_LOWER_IL_INJECTED


class WindowsHotkeyRig(PynputHotkeyRig):
    """基于 `_hotkey.parse_hotkey_win32` + `PynputHotkeyRig` 的 Windows 五键监听器（含低级钩子吞键）。"""

    def __init__(
        self,
        bindings: HotkeyBindings,
        *,
        on_voice_start: Callable[[], bool],
        on_voice_stop: Callable[[], None],
        on_screenshot: Callable[[], None],
        on_ocr: Callable[[], None] = noop,
        on_repeat: Callable[[], None] = noop,
        on_annotate: Callable[[], None] = noop,
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
        listener_factory: Callable[..., object] | None = None,
        modifier_state_reader: Callable[[], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved_reader = modifier_state_reader
        if resolved_reader is None and listener_factory is None and os.name == "nt":
            resolved_reader = read_win32_modifier_mask
        super().__init__(
            bindings,
            backend_name="win32-pynput",
            hotkey_parser=parse_hotkey_win32,
            key_extractor=_extract_win32_key_info,
            is_valid_code=lambda vk: vk > 0,
            on_voice_start=on_voice_start,
            on_voice_stop=on_voice_stop,
            on_screenshot=on_screenshot,
            on_ocr=on_ocr,
            on_repeat=on_repeat,
            on_annotate=on_annotate,
            overlay_active=overlay_active,
            close_overlay=close_overlay,
            listener_factory=listener_factory,
            modifier_state_reader=resolved_reader,
            clock=clock,
        )

    def _listener_kwargs(self) -> dict[str, object]:
        return {"win32_event_filter": self._win32_event_filter}

    def _win32_event_filter(self, msg: int, data) -> bool:
        """WH_KEYBOARD_LL 过滤器：拦截匹配的应用热键并跳过多余 `_WM_PROCESS`；不匹配时返回 False 放行。"""
        if msg not in _WM_PRESS_MESSAGES and msg not in _WM_RELEASE_MESSAGES:
            return False
        flags = int(getattr(data, "flags", 0) or 0)
        vk = int(getattr(data, "vkCode", 0) or 0)
        if vk == _VK_PACKET or (flags & _LLKHF_ANY_INJECTED) != 0:
            return False
        mod_flag = _VK_TO_WIN32_MOD.get(vk, 0)
        if msg in _WM_PRESS_MESSAGES:
            should_suppress = self._handle_press_raw(mod_flag, vk)
        else:
            should_suppress = self._handle_release_raw(mod_flag, vk)
        if should_suppress:
            listener = self._listener
            suppress_fn = getattr(listener, "suppress_event", None) if listener is not None else None
            if callable(suppress_fn):
                suppress_fn()
        return False


class WindowsPlatformAdapter:
    os_name = "windows"
    session_type = "windows"

    def __init__(
        self,
        *,
        win32_api: Win32Api | None = None,
        mss_factory=None,
        listener_factory: Callable[..., object] | None = None,
        modifier_state_reader: Callable[[], int | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        prefer_unicode: bool = True,
        which: Callable[[str], str | None] = shutil.which,
        ocr_languages: Callable[[], list[str]] | None = None,
        reg_api=None,
    ) -> None:
        self._win32_api = win32_api
        self._mss_factory = mss_factory
        self._listener_factory = listener_factory
        self._modifier_state_reader = modifier_state_reader
        self._sleep = sleep
        self._prefer_unicode = prefer_unicode
        self._which = which
        self._ocr_languages = ocr_languages
        self._reg_api = reg_api
        self._inject_lock = threading.Lock()

    def _win32(self) -> Win32Api:
        if self._win32_api is None:
            self._win32_api = CtypesWin32Api()
        return self._win32_api

    def capture_screen(self) -> ScreenCapture:
        return capture_screen_mss(self._mss_factory)

    def current_focus(self) -> FocusTarget:
        api = self._win32()
        hwnd = api.get_foreground_window()
        if not hwnd:
            return FocusTarget(focus_id="", app_id="", is_terminal=False, is_self=False)
        pid = api.get_window_pid(hwnd)
        raw_exe = api.get_process_image_name(pid)
        exe_name = os.path.basename(raw_exe.replace("\\", "/")) if raw_exe else ""
        title = api.get_window_title(hwnd) or ""
        is_self = bool(
            (pid and pid == api.get_current_pid())
            or "ollama-voice-input" in title.lower()
            or exe_name.lower() == "ollama-voice-input.exe"
        )
        is_terminal = exe_name.lower() in WINDOWS_TERMINAL_EXES
        return FocusTarget(
            focus_id=str(hwnd),
            app_id=exe_name,
            is_terminal=is_terminal,
            is_self=is_self,
        )

    def read_clipboard_text(self) -> str:
        return self._win32().read_clipboard_text()

    def write_clipboard_text(self, text: str) -> str | None:
        return self._win32().write_clipboard_text(text)

    def write_clipboard_png(self, png: bytes) -> str | None:
        if not png:
            return "剪贴板图片为空"
        try:
            dib = _png_to_dib(png)
        except Exception as exc:
            return f"PNG 转 DIB 失败: {exc}"
        return self._win32().write_clipboard_dib(dib)

    def inject_text(
        self,
        text: str,
        *,
        target: FocusTarget | None = None,
        terminal: bool | None = None,
    ) -> PasteResult:
        blocked = check_focus_and_self_guard(target)
        if blocked is not None:
            return blocked

        with self._inject_lock:
            api = self._win32()
            live = self.current_focus()
            blocked = check_focus_and_self_guard(target, live)
            if blocked is not None:
                return blocked

            use_terminal = (
                terminal
                if terminal is not None
                else bool((target and target.is_terminal) or live.is_terminal)
            )

            if self._prefer_unicode and not use_terminal and "\n" not in text and "\r" not in text:
                try:
                    if api.send_unicode_text(text):
                        return PasteResult(True, "sendinput_unicode", "已通过 SendInput(KEYEVENTF_UNICODE) 直接输入")
                except Exception as exc:
                    return PasteResult(False, "sendinput_unicode", f"SendInput 途中异常: {exc}", uncertain=True)

            snap = api.snapshot_clipboard()
            err = api.write_clipboard_text(text)
            if err is not None:
                return PasteResult(False, "clipboard", err, uncertain=False)

            vk_v = KEY_TABLE["v"].vk
            combo = (VK_CONTROL, VK_SHIFT, vk_v) if use_terminal else (VK_CONTROL, vk_v)
            try:
                sent = api.send_vk_combo(combo)
            except Exception as exc:
                self._restore_if_unchanged(api, snap, text)
                return PasteResult(False, "clipboard_paste", f"粘贴按键异常: {exc}", uncertain=True)

            if not sent:
                self._restore_if_unchanged(api, snap, text)
                return PasteResult(False, "clipboard_paste", "SendInput 发送粘贴组合键失败", uncertain=False)

            self._sleep(PASTE_RESTORE_DELAY_S)
            self._restore_if_unchanged(api, snap, text)
            method = "ctrl_shift_v" if use_terminal else "ctrl_v"
            return PasteResult(True, method, "已通过剪贴板粘贴并恢复原剪贴板")

    @staticmethod
    def _restore_if_unchanged(api: Win32Api, snap: WinClipboardSnapshot, injected: str) -> None:
        if snap.kind in ("unsupported", "failed"):
            return
        try:
            if api.read_clipboard_text() == injected:
                api.restore_clipboard(snap)
        except Exception:
            pass

    def create_hotkey_rig(
        self,
        bindings: HotkeyBindings,
        *,
        on_voice_start: Callable[[], bool],
        on_voice_stop: Callable[[], None],
        on_screenshot: Callable[[], None],
        on_ocr: Callable[[], None] = noop,
        on_repeat: Callable[[], None] = noop,
        on_annotate: Callable[[], None] = noop,
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
        on_error: Callable[[str | None], None],
    ) -> HotkeyRig:
        del on_error  # 协议要求的组件失败通道；pynput 监听器没有组件级运行期失败，不调用
        return WindowsHotkeyRig(
            bindings,
            on_voice_start=on_voice_start,
            on_voice_stop=on_voice_stop,
            on_screenshot=on_screenshot,
            on_ocr=on_ocr,
            on_repeat=on_repeat,
            on_annotate=on_annotate,
            overlay_active=overlay_active,
            close_overlay=close_overlay,
            listener_factory=self._listener_factory,
            modifier_state_reader=self._modifier_state_reader,
        )

    @staticmethod
    def autostart_registry_entry(
        command: Sequence[str],
        *,
        app_name: str = "OllamaVoiceInput",
    ) -> dict[str, str]:
        quoted = " ".join(f'"{arg}"' if (" " in str(arg) or "\t" in str(arg)) else str(arg) for arg in command)
        return {
            "hive": "HKCU",
            "key": RUN_REGISTRY_KEY,
            "name": app_name,
            "value": quoted,
        }

    @staticmethod
    def render_startup_script(command: Sequence[str], *, workdir: Path) -> str:
        """生成无黑框后台启动 VBS 辅助脚本（可供开机启动文件夹或计划任务调用）。"""
        ps_args = " ".join(f'""{arg}""' if " " in str(arg) else str(arg) for arg in command)
        wdir = str(workdir).replace('"', '""')
        return (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.CurrentDirectory = "{wdir}"\n'
            f'WshShell.Run "{ps_args}", 0, False\n'
        )

    def configure_autostart(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        app_name: str = "OllamaVoiceInput",
    ) -> None:
        spec = self.autostart_registry_entry(command, app_name=app_name)
        reg = self._reg_api
        if reg is None:
            import winreg as reg  # type: ignore[import-not-found]

        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_REGISTRY_KEY, 0, reg.KEY_SET_VALUE) as key:
            if enabled:
                reg.SetValueEx(key, app_name, 0, reg.REG_SZ, spec["value"])
            else:
                try:
                    reg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass

    def capabilities(self) -> list[SetupCapability]:
        win32_ok = True
        win32_detail = "Win32 user32/SendInput 就绪"
        try:
            self._win32()
        except Exception as exc:
            win32_ok = False
            win32_detail = str(exc)

        mss_ok = True
        mss_detail = "mss 虚拟桌面抓屏就绪"
        if self._mss_factory is None:
            try:
                import mss  # noqa: F401
            except Exception as exc:
                mss_ok = False
                mss_detail = str(exc)

        pynput_ok = True
        pynput_detail = "pynput 全局热键就绪"
        if self._listener_factory is None:
            try:
                import pynput  # noqa: F401
            except Exception as exc:
                pynput_ok = False
                pynput_detail = str(exc)

        caps = [
            SetupCapability(id="win32_input", label="Windows 输入与剪贴板 (Win32)", available=win32_ok, required=True, detail=win32_detail),
            SetupCapability(id="win32_capture", label="Windows 多屏抓屏 (mss)", available=mss_ok, required=True, detail=mss_detail),
            SetupCapability(id="win32_hotkeys", label="Windows 全局热键监听", available=pynput_ok, required=True, detail=pynput_detail),
        ]
        caps.extend(probe_ocr_capabilities(self._ocr_languages, missing_prefix="tesseract 缺少"))
        return caps

    def play_beep(self, freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
        common_play_beep(freq=freq, duration=duration, volume=volume)

    def close(self) -> None:
        return None


__all__ = [
    "CF_DIB",
    "CF_HDROP",
    "CF_UNICODETEXT",
    "PASTE_RESTORE_DELAY_S",
    "RUN_REGISTRY_KEY",
    "VK_CONTROL",
    "VK_LWIN",
    "VK_MENU",
    "VK_RWIN",
    "VK_SHIFT",
    "VK_V",
    "WINDOWS_TERMINAL_EXES",
    "CtypesWin32Api",
    "Win32Api",
    "WinClipboardSnapshot",
    "WindowsHotkeyRig",
    "WindowsPlatformAdapter",
    "_configure_win32_signatures",
    "_extract_win32_key_info",
    "read_win32_modifier_mask",
]
