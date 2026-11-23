"""Linux / Windows / macOS 三端 PlatformAdapter 统一协议契约单测（100% 离线/注入驱动，不触碰真机窗口）。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import plistlib
import subprocess
import threading
import time
from pathlib import Path

import pytest

from _hotkey import KEY_TABLE, MACOS_MODIFIERS, HotkeyBindings
from _linux.ibus import IBusCommitter
from _linux.modifiers import ModifierJanitor
from _linux.paste_backend import FocusedWindowInfo
from _platform._common import play_beep as common_play_beep, png_dimensions
from _platform._win32_api import (
    CF_DIB,
    CF_HDROP,
    CF_UNICODETEXT,
    CtypesWin32Api,
    _configure_win32_signatures,
    set_windows_app_user_model_id,
)
from _platform.detect import create_platform_adapter, detect_os
from _platform.linux import LinuxPlatformAdapter
from _platform.macos import (
    KVK_ANSI_V,
    MACOS_TERMINAL_BUNDLES,
    MacClipboardSnapshot,
    MacOSHotkeyRig,
    MacOSPlatformAdapter,
    RealMacOSSystemApi,
    _extract_macos_key_info,
)
from _platform.protocol import (
    FocusTarget,
    HotkeyRig,
    PasteResult,
    PlatformAdapter,
    ScreenCapture,
)
from _platform.windows import (
    VK_CONTROL,
    VK_SHIFT,
    VK_V,
    WINDOWS_TERMINAL_EXES,
    WinClipboardSnapshot,
    WindowsPlatformAdapter,
)

BINDINGS = HotkeyBindings(
    voice="ctrl+alt+z",
    screenshot="ctrl+alt+x",
    ocr="ctrl+alt+o",
    repeat="ctrl+alt+r",
)


class _FakeMssShot:
    def __init__(self, size=(20, 10)):
        self.size = size
        self.rgb = b"\x11\x22\x33" * (size[0] * size[1])


class _FakeMss:
    def __init__(self, virtual_mon=None):
        self.monitors = [
            virtual_mon or {"left": -1920, "top": 0, "width": 3840, "height": 1080},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def grab(self, _mon):
        return _FakeMssShot((16, 8))


class _FakeListener:
    def __init__(self, on_press, on_release):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FakeKey:
    def __init__(self, *, name: str | None = None, char: str | None = None, vk: int | None = None):
        self.name = name
        self.char = char
        self.vk = vk


class _FakeWinFn:
    """Win32 DLL 函数桩：记录 argtypes/restype 并将调用转给可注入 impl。"""

    def __init__(self, impl=None):
        self.impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.impl(*args) if self.impl else 0


class _FakeWinDll:
    """Win32 DLL 容器桩（供 CtypesWin32Api 离线契约测试复用）。"""

    def __init__(self, mapping):
        self._m = {k: _FakeWinFn(v) for k, v in mapping.items()}

    def __getattr__(self, name: str):
        return self._m.setdefault(name, _FakeWinFn())


# ---------------------------------------------------------------------------
# 1. detect_os & create_platform_adapter 契约测试
# ---------------------------------------------------------------------------


def test_detect_os_and_factory_protocol():
    assert detect_os("linux") == "linux"
    assert detect_os("win32") == "windows"
    assert detect_os("cygwin") == "windows"
    assert detect_os("darwin") == "macos"
    with pytest.raises(RuntimeError):
        detect_os("freebsd99")

    import _platform as platform_pkg
    from _platform import protocol as protocol_mod

    linux_adapter = create_platform_adapter(os_name="linux", environ={"XDG_SESSION_TYPE": "x11"}, uinput_factory=None)
    win_adapter = create_platform_adapter(os_name="windows")
    mac_adapter = create_platform_adapter(os_name="macos")
    for adapter in (linux_adapter, win_adapter, mac_adapter):
        assert isinstance(adapter, PlatformAdapter)
        assert hasattr(adapter, "capture_screen")
        assert not hasattr(adapter, "capture_fullscreen")
        assert not hasattr(adapter, "capture_region")
    assert not hasattr(PlatformAdapter, "capture_fullscreen")
    assert not hasattr(PlatformAdapter, "capture_region")
    assert hasattr(platform_pkg, "capture_screen_mss")
    assert hasattr(protocol_mod, "capture_screen_mss")
    assert not hasattr(platform_pkg, "capture_region_mss")
    assert not hasattr(protocol_mod, "capture_region_mss")


# ---------------------------------------------------------------------------
# 2. LinuxPlatformAdapter 契约测试（X11 mss 全屏抓屏、Wayland Portal 整屏与回退 mss、注入与四键）
# ---------------------------------------------------------------------------


def test_linux_adapter_capture_focus_inject_and_hotkeys(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (12, 34, 56)).save(buf, format="PNG")
    portal_png = buf.getvalue()

    # X11 直接使用 mss 抓取全虚拟桌面（monitors[0]）
    adapter_x11 = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "x11"},
        mss_factory=lambda: _FakeMss({"left": -1920, "top": -120, "width": 3840, "height": 1200}),
        window_probe=lambda: FocusedWindowInfo(window_id=501, wm_class=("kitty", "kitty"), is_terminal=True),
        uinput_factory=None,
    )
    shot_x11 = adapter_x11.capture_screen()
    assert isinstance(shot_x11, ScreenCapture)
    assert shot_x11.png_bytes.startswith(b"\x89PNG")
    assert shot_x11.virtual_rect == (-1920, -120, 3840, 1200)
    focus = adapter_x11.current_focus()
    assert focus == FocusTarget(focus_id="501", app_id="kitty", is_terminal=True, is_self=False)

    # Wayland 优先 Portal 整屏抓拍，Portal 异常自动回退 mss
    adapter_wl_ok = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=lambda: ScreenCapture(png_bytes=portal_png, virtual_rect=(0, 0, 64, 36)),
        mss_factory=_FakeMss,
        uinput_factory=None,
    )
    wl_shot = adapter_wl_ok.capture_screen()
    assert wl_shot == ScreenCapture(png_bytes=portal_png, virtual_rect=(0, 0, 64, 36))

    def _fail_portal():
        raise RuntimeError("portal denied")

    adapter_wl_fb = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=_fail_portal,
        mss_factory=lambda: _FakeMss({"left": 0, "top": 0, "width": 1920, "height": 1080}),
        uinput_factory=None,
    )
    shot_fb = adapter_wl_fb.capture_screen()
    assert shot_fb.png_bytes.startswith(b"\x89PNG")
    assert shot_fb.virtual_rect == (0, 0, 1920, 1080)

    # Linux 注入：焦点不匹配拦截、自窗口拦截、以及正常粘贴
    state = {"focus": FocusedWindowInfo(window_id=501, wm_class=("firefox", "Firefox"))}
    pasted_cmds: list[list[str]] = []

    class _MemClip:
        def __init__(self):
            self.val = "orig"

        def read(self):
            return self.val

        def write(self, t):
            self.val = t
            return None

    class _FakeMgr:
        backend_name = "portal"

        def __init__(self, ctx, *, on_error):
            self.ctx = ctx
            self.on_error = on_error
            self.started = False
            self.cleaned = False

        def start(self):
            self.started = True

        def cleanup(self):
            self.cleaned = True

    adapter_inj = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda n: f"/usr/bin/{n}" if n in {"xclip", "xdotool"} else None,
        run=lambda cmd, **kw: (pasted_cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")),
        sleep=lambda _s: None,
        window_probe=lambda: state["focus"],
        clipboard=_MemClip(),  # type: ignore[arg-type]
        ibus=IBusCommitter(None, available=False),
        janitor=ModifierJanitor(session_type="x11", kb=None, key=None, xdotool=""),
        hotkey_manager_cls=_FakeMgr,
        ocr_languages=lambda: ["chi_sim", "chi_tra", "eng"],
    )
    mismatch = adapter_inj.inject_text(
        "hello", target=FocusTarget(focus_id="999", app_id="other", is_terminal=False, is_self=False)
    )
    assert mismatch.ok is False and mismatch.method == "focus_mismatch" and mismatch.ack_result == "none"

    ok_res = adapter_inj.inject_text("hello", target=FocusTarget(focus_id="501", app_id="Firefox", is_terminal=False, is_self=False))
    assert ok_res.ok is True and ok_res.ack_result == "complete"
    assert pasted_cmds == [["/usr/bin/xdotool", "key", "ctrl+v"]]

    state["focus"] = FocusedWindowInfo(window_id=777, wm_class=("ollama-voice-input",), is_self=True)
    blocked = adapter_inj.inject_text("hello")
    assert blocked.ok is False and blocked.method == "self_window_blocked" and blocked.ack_result == "none"

    ocr_triggered = []
    rig = adapter_inj.create_hotkey_rig(
        BINDINGS,
        on_voice_start=lambda: True,
        on_voice_stop=lambda: None,
        on_screenshot=lambda: None,
        on_ocr=lambda: ocr_triggered.append(True),
        on_error=lambda _reason: None,
    )
    assert isinstance(rig, HotkeyRig)
    rig.start()
    rig._manager.ctx.actions.ocr()  # type: ignore[attr-defined]
    assert ocr_triggered == [True]
    rig.stop()
    caps = adapter_inj.capabilities(BINDINGS)
    assert any(c.id == "ocr_chi_sim" and c.available for c in caps)
    desktop_entry = adapter_inj.render_autostart_desktop(["/opt/ovi/daemon"], workdir=Path("/opt/ovi"))
    assert "Exec=/opt/ovi/daemon" in desktop_entry
    adapter_inj.configure_autostart(enabled=True, command=["/opt/ovi/py", "/opt/ovi/daemon.py", "--background"],
                                    config_home=tmp_path / "cfg")
    entry = tmp_path / "cfg" / "autostart" / "ollama-voice-input.desktop"
    assert "Exec=/opt/ovi/py /opt/ovi/daemon.py --background" in entry.read_text(encoding="utf-8")
    adapter_inj.configure_autostart(enabled=False, command=[], config_home=tmp_path / "cfg")
    assert not entry.exists()


# ---------------------------------------------------------------------------
# 3. WindowsPlatformAdapter 契约测试
# ---------------------------------------------------------------------------


class _FakeWin32Api:
    def __init__(self) -> None:
        self.self_pid = 4000
        self.hwnd = 1001
        self.win_pid = 8888
        self.exe = r"C:\Program Files\Mozilla Firefox\firefox.exe"
        self.title = "Mozilla Firefox"
        self.unicode_sent: list[str] = []
        self.vk_combos: list[tuple[int, ...]] = []
        self.clipboard = WinClipboardSnapshot(kind="text", text="win_orig")
        self.fail_vk_exc: Exception | None = None
        self.fail_clip: str | None = None

    def get_current_pid(self) -> int:
        return self.self_pid

    def get_foreground_window(self) -> int:
        return self.hwnd

    def get_window_pid(self, _hwnd: int) -> int:
        return self.win_pid

    def get_window_title(self, _hwnd: int) -> str:
        return self.title

    def get_process_image_name(self, _pid: int) -> str:
        return self.exe

    def send_unicode_text(self, text: str) -> bool:
        self.unicode_sent.append(text)
        return True

    def send_vk_combo(self, vk_codes) -> bool:
        if self.fail_vk_exc is not None:
            raise self.fail_vk_exc
        self.vk_combos.append(tuple(vk_codes))
        return True

    def read_clipboard_text(self) -> str:
        return self.clipboard.text if self.clipboard.kind == "text" else ""

    def write_clipboard_text(self, text: str) -> str | None:
        if self.fail_clip:
            return self.fail_clip
        self.clipboard = WinClipboardSnapshot(kind="text", text=text)
        return None

    def write_clipboard_dib(self, dib: bytes) -> str | None:
        if self.fail_clip:
            return self.fail_clip
        self.clipboard = WinClipboardSnapshot(kind="dib", dib_bytes=dib)
        return None

    def snapshot_clipboard(self) -> WinClipboardSnapshot:
        return self.clipboard

    def restore_clipboard(self, snap: WinClipboardSnapshot) -> None:
        self.clipboard = snap


def test_windows_adapter_focus_terminals_sendinput_clipboard_hotkeys_and_autostart(tmp_path: Path):
    api = _FakeWin32Api()
    sleeps: list[float] = []
    listeners: list[_FakeListener] = []

    class _FakeRegKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    reg_store: dict[str, str] = {}

    class _FakeWinReg:
        HKEY_CURRENT_USER = 1
        KEY_SET_VALUE = 2
        REG_SZ = 1

        @staticmethod
        def OpenKey(*_a):
            return _FakeRegKey()

        @staticmethod
        def SetValueEx(_k, name, _r, _t, val):
            reg_store[name] = val

        @staticmethod
        def DeleteValue(_k, name):
            if name not in reg_store:
                raise FileNotFoundError(name)
            del reg_store[name]

    adapter = WindowsPlatformAdapter(
        win32_api=api,
        mss_factory=lambda: _FakeMss({"left": -1920, "top": 0, "width": 4480, "height": 1440}),
        listener_factory=lambda on_press, on_release: (
            listeners.append(_FakeListener(on_press, on_release)) or listeners[-1]
        ),
        sleep=sleeps.append,
        ocr_languages=lambda: ["chi_sim", "chi_tra", "eng"],
        reg_api=_FakeWinReg,
    )

    # 1. 多屏虚拟桌面全屏抓屏
    cap = adapter.capture_screen()
    assert isinstance(cap, ScreenCapture)
    assert cap.png_bytes.startswith(b"\x89PNG")
    assert cap.virtual_rect == (-1920, 0, 4480, 1440)

    # 2. 终端进程名全集识别
    for term_exe in (
        "WindowsTerminal.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "mintty.exe",
    ):
        assert term_exe.lower() in WINDOWS_TERMINAL_EXES
        api.exe = rf"C:\Tools\{term_exe}"
        ft = adapter.current_focus()
        assert ft.is_terminal is True and ft.app_id == term_exe

    # 3. 常规应用单行文本 → SendInput(KEYEVENTF_UNICODE) 直输，不碰剪贴板
    api.exe = r"C:\Program Files\Notepad++\notepad++.exe"
    res_uni = adapter.inject_text("你好 Windows 🚀")
    assert res_uni.ok is True and res_uni.method == "sendinput_unicode"
    assert api.unicode_sent == ["你好 Windows 🚀"]
    assert api.clipboard == WinClipboardSnapshot(kind="text", text="win_orig")

    # 4. 终端窗口 + 原剪贴板为 DIB 位图 → Ctrl+Shift+V 粘贴并在 0.55s 后恢复原位图剪贴板
    dib_payload = b"FAKE_WIN_DIB_SCREENSHOT_BYTES"
    api.clipboard = WinClipboardSnapshot(kind="dib", dib_bytes=dib_payload)
    api.exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    res_term = adapter.inject_text("Get-Process")
    assert res_term.ok is True and res_term.method == "ctrl_shift_v"
    assert api.vk_combos[-1] == (VK_CONTROL, VK_SHIFT, VK_V)
    assert sleeps[-1] == 0.55
    assert api.clipboard == WinClipboardSnapshot(kind="dib", dib_bytes=dib_payload)

    # 5. 多行文本走 Ctrl+V 剪贴板粘贴
    api.exe = r"C:\Program Files\Code\Code.exe"
    api.clipboard = WinClipboardSnapshot(kind="text", text="before_code")
    res_ml = adapter.inject_text("line1\nline2")
    assert res_ml.ok is True and res_ml.method == "ctrl_v"
    assert api.vk_combos[-1] == (VK_CONTROL, VK_V)
    assert api.read_clipboard_text() == "before_code"

    # 6. 自窗口拦截与中途异常 partial_or_unknown
    api.win_pid = api.self_pid
    assert adapter.inject_text("nope").method == "self_window_blocked"
    api.win_pid = 8888
    api.fail_vk_exc = RuntimeError("SendInput interrupted")
    res_uncertain = adapter.inject_text("line1\nline2")
    assert res_uncertain.ok is False and res_uncertain.uncertain is True and res_uncertain.ack_result == "partial_or_unknown"
    api.fail_vk_exc = None

    # 7. 四键监听（含修饰键与自身热键不误关浮窗、普通键/Esc 关闭浮窗、voice 按住/重复过滤/松开）
    events: list[str] = []
    overlay_open = {"v": True}
    rig = adapter.create_hotkey_rig(
        BINDINGS,
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
        overlay_active=lambda: overlay_open["v"],
        close_overlay=lambda: (overlay_open.__setitem__("v", False) or events.append("close_ov") or True),
        on_error=lambda _reason: None,
    )
    rig.start()
    lst = listeners[-1]
    assert lst.started is True
    lst.on_press(_FakeKey(name="ctrl_l", vk=0xA2))
    lst.on_press(_FakeKey(name="alt_l", vk=0xA4))
    assert "close_ov" not in events and overlay_open["v"] is True
    lst.on_press(_FakeKey(char="z", vk=0x5A))
    lst.on_press(_FakeKey(char="z", vk=0x5A))  # 自动重复忽略
    lst.on_release(_FakeKey(char="z", vk=0x5A))
    lst.on_press(_FakeKey(char="x", vk=0x58))
    lst.on_release(_FakeKey(char="x", vk=0x58))
    lst.on_press(_FakeKey(char="o", vk=0x4F))
    lst.on_release(_FakeKey(char="o", vk=0x4F))
    lst.on_press(_FakeKey(char="r", vk=0x52))
    lst.on_release(_FakeKey(char="r", vk=0x52))
    lst.on_release(_FakeKey(name="alt_l", vk=0xA4))
    lst.on_release(_FakeKey(name="ctrl_l", vk=0xA2))
    assert events == ["v_start", "v_stop", "shot", "ocr", "repeat"]
    assert overlay_open["v"] is True
    # 非修饰键且未命中自身热键（如 Esc）触发关闭浮窗
    lst.on_press(_FakeKey(name="esc", vk=0x1B))
    lst.on_release(_FakeKey(name="esc", vk=0x1B))
    assert overlay_open["v"] is False and events[-1] == "close_ov"
    rig.stop()
    assert lst.stopped is True

    # 8. 开机自启注册表 & VBS 脚本 & capabilities
    command = [r"C:\Program Files\Python\pythonw.exe", "daemon.py"]
    assert adapter.configure_autostart(enabled=True, command=command) is None
    assert reg_store["OllamaVoiceInput"] == adapter.autostart_registry_entry(command)["value"]
    adapter.configure_autostart(enabled=False, command=["pythonw.exe", "daemon.py"])
    assert "OllamaVoiceInput" not in reg_store
    vbs = adapter.render_startup_script(["pythonw.exe", "daemon.py"], workdir=tmp_path)
    assert "WScript.Shell" in vbs
    caps = adapter.capabilities()
    assert all(c.available for c in caps)


# ---------------------------------------------------------------------------
# 4. MacOSPlatformAdapter 契约测试
# ---------------------------------------------------------------------------


class _FakeMacOSApi:
    def __init__(self) -> None:
        self.self_pid = 5000
        self.bundle_id = "com.apple.Safari"
        self.pid = 6001
        self.app_name = "Safari"
        self.clip = MacClipboardSnapshot(kind="text", text="mac_orig")
        self.cmd_v_posts = 0
        self.cmd_v_ok = True
        self.cmd_v_exc: Exception | None = None
        self.ax_trusted = True
        self.sr_allowed = True
        self.screen_calls: list[Path] = []

    def get_current_pid(self) -> int:
        return self.self_pid

    def frontmost_app(self) -> tuple[str, int, str]:
        return self.bundle_id, self.pid, self.app_name

    def capture_screen(self, temp_dir: Path) -> ScreenCapture:
        assert not str(temp_dir).startswith("/tmp")
        self.screen_calls.append(temp_dir)
        return ScreenCapture(png_bytes=b"\x89PNG_MAC_RETINA", virtual_rect=(0, 0, 1512, 982))

    def read_clipboard_text(self) -> str:
        return self.clip.text if self.clip.kind == "text" else ""

    def write_clipboard_text(self, text: str) -> str | None:
        self.clip = MacClipboardSnapshot(kind="text", text=text)
        return None

    def write_clipboard_png(self, png: bytes, temp_dir: Path) -> str | None:
        assert not str(temp_dir).startswith("/tmp")
        self.clip = MacClipboardSnapshot(kind="png", png_bytes=png)
        return None

    def snapshot_clipboard(self, _temp_dir: Path) -> MacClipboardSnapshot:
        return self.clip

    def restore_clipboard(self, snap: MacClipboardSnapshot, _temp_dir: Path) -> None:
        self.clip = snap

    def post_cmd_v(self) -> bool:
        if self.cmd_v_exc is not None:
            raise self.cmd_v_exc
        self.cmd_v_posts += 1
        return self.cmd_v_ok

    def is_accessibility_trusted(self) -> bool:
        return self.ax_trusted

    def is_screen_recording_allowed(self) -> bool:
        return self.sr_allowed


def test_macos_adapter_bundles_paste_hotkeys_plist_and_permissions(tmp_path: Path):
    api = _FakeMacOSApi()
    sleeps: list[float] = []
    listeners: list[_FakeListener] = []

    adapter = MacOSPlatformAdapter(
        macos_api=api,
        listener_factory=lambda on_press, on_release: (
            listeners.append(_FakeListener(on_press, on_release)) or listeners[-1]
        ),
        sleep=sleeps.append,
        temp_dir=tmp_path / "mac_tmp",
        ocr_languages=lambda: ["chi_sim", "chi_tra", "eng"],
    )

    # 1. 全屏抓屏（不再存在废弃 capture_fullscreen / capture_region）
    assert not hasattr(adapter, "capture_fullscreen")
    assert not hasattr(adapter, "capture_region")
    cap = adapter.capture_screen()
    assert cap == ScreenCapture(png_bytes=b"\x89PNG_MAC_RETINA", virtual_rect=(0, 0, 1512, 982))
    assert api.screen_calls == [tmp_path / "mac_tmp"]

    # 2. 终端 Bundle ID 识别
    for bid in (
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "net.kovidgoyal.kitty",
        "io.alacritty",
        "com.github.wez.wezterm",
    ):
        assert bid.lower() in MACOS_TERMINAL_BUNDLES
        api.bundle_id = bid
        ft = adapter.current_focus()
        assert ft.is_terminal is True and ft.app_id == bid

    # 3. PNG 剪贴板保护 + CGEvent Cmd+V 注入 + 0.55s 恢复
    api.bundle_id = "com.apple.Safari"
    orig_png = b"\x89PNG_MAC_CLIP"
    api.clip = MacClipboardSnapshot(kind="png", png_bytes=orig_png)
    res = adapter.inject_text("你好 macOS")
    assert res.ok is True and res.method == "cgevent_cmd_v"
    assert api.cmd_v_posts == 1
    assert sleeps[-1] == 0.55
    assert api.clip == MacClipboardSnapshot(kind="png", png_bytes=orig_png)

    # 4. 自窗口拦截、无辅助功能权限与异常分支
    api.pid = api.self_pid
    assert adapter.inject_text("self").method == "self_window_blocked"
    api.pid = 6001
    api.cmd_v_ok = False
    denied = adapter.inject_text("denied")
    assert denied.ok is False and denied.uncertain is False and "辅助功能" in denied.detail
    api.cmd_v_ok = True
    api.cmd_v_exc = RuntimeError("Quartz tap error")
    uncertain = adapter.inject_text("boom")
    assert uncertain.ok is False and uncertain.uncertain is True and uncertain.ack_result == "partial_or_unknown"
    api.cmd_v_exc = None

    # 5. macOS 四键监听（含 Command 键位与按住说话）
    mac_bindings = HotkeyBindings(
        voice="super+alt+z",
        screenshot="super+shift+x",
        ocr="super+shift+o",
        repeat="super+alt+r",
    )
    events: list[str] = []
    rig = adapter.create_hotkey_rig(
        mac_bindings,
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
        on_error=lambda _reason: None,
    )
    rig.start()
    lst = listeners[-1]
    # super(0x37) + alt(0x3A) + z(0x06)
    lst.on_press(_FakeKey(name="cmd", vk=0x37))
    lst.on_press(_FakeKey(name="alt", vk=0x3A))
    lst.on_press(_FakeKey(char="z", vk=0x06))
    lst.on_release(_FakeKey(name="cmd", vk=0x37))  # 松开修饰键也应立即停止录音防卡死！
    lst.on_release(_FakeKey(char="z", vk=0x06))
    lst.on_release(_FakeKey(name="alt", vk=0x3A))

    # super(0x37) + shift(0x38) + x(0x07) / o(0x1F)
    lst.on_press(_FakeKey(name="cmd", vk=0x37))
    lst.on_press(_FakeKey(name="shift", vk=0x38))
    lst.on_press(_FakeKey(char="x", vk=0x07))
    lst.on_release(_FakeKey(char="x", vk=0x07))
    lst.on_press(_FakeKey(char="o", vk=0x1F))
    lst.on_release(_FakeKey(char="o", vk=0x1F))
    lst.on_release(_FakeKey(name="shift", vk=0x38))

    # super(0x37) + alt(0x3A) + r(0x0F)
    lst.on_press(_FakeKey(name="alt", vk=0x3A))
    lst.on_press(_FakeKey(char="r", vk=0x0F))
    lst.on_release(_FakeKey(char="r", vk=0x0F))
    lst.on_release(_FakeKey(name="alt", vk=0x3A))
    lst.on_release(_FakeKey(name="cmd", vk=0x37))
    assert events == ["v_start", "v_stop", "shot", "ocr", "repeat"]
    rig.stop()

    # 6. LaunchAgents plist 生成与写入/移除 & 权限自检
    adapter.configure_autostart(
        enabled=True,
        command=["/opt/ovi/.venv/bin/python", "/opt/ovi/daemon.py"],
        workdir=Path("/opt/ovi"),
        log_dir=tmp_path / "logs",
        home=tmp_path,
    )
    plist_file = adapter.launch_agent_path(home=tmp_path)
    assert plist_file.is_file()
    parsed_plist = plistlib.loads(plist_file.read_bytes())
    assert parsed_plist["Label"] == "com.ollama-voice-input.daemon"
    assert parsed_plist["RunAtLoad"] is True and parsed_plist["KeepAlive"] is True
    adapter.configure_autostart(enabled=False, command=["python"], home=tmp_path)
    assert not plist_file.exists()

    api.ax_trusted = False
    api.sr_allowed = True
    caps = {c.id: c for c in adapter.capabilities()}
    assert caps["macos_accessibility"].available is False
    assert caps["macos_screen_recording"].available is True


def test_real_macos_system_api_lsappinfo_and_screencapture_fallback(tmp_path: Path):
    """验证 RealMacOSSystemApi 的 lsappinfo 解析与 screencapture 临时目录清理（不写 /tmp）。"""
    import struct
    import zlib

    def _minimal_png(w: int, h: int) -> bytes:
        def _chunk(tag: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

        raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b"")
        )

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        del kw
        seen_cmds.append(list(cmd))
        if cmd == ["lsappinfo", "front"]:
            return subprocess.CompletedProcess(cmd, 0, "ASN:0x0-0x2f02f:", "")
        if cmd[:2] == ["lsappinfo", "info"]:
            out = '"CFBundleIdentifier"="com.googlecode.iterm2"\n"pid"=7312\n"LSDisplayName"="iTerm2"\n'
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[0] == "screencapture":
            Path(cmd[-1]).write_bytes(_minimal_png(640, 480))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    def broken_mss():
        raise RuntimeError("mss denied")

    sys_api = RealMacOSSystemApi(
        run=fake_run,
        popen=lambda *a, **kw: type("P", (), {"returncode": 0, "stdin": io.BytesIO(), "communicate": lambda self, *x, **y: (b"", b"")})(),
        which=lambda name: f"/usr/bin/{name}" if name in {"lsappinfo", "screencapture", "pbcopy", "pbpaste"} else None,
        mss_factory=broken_mss,
    )
    assert not hasattr(sys_api, "capture_region")
    assert sys_api.frontmost_app() == ("com.googlecode.iterm2", 7312, "iTerm2")
    work_tmp = tmp_path / "shot_tmp"
    shot = sys_api.capture_screen(work_tmp)
    assert isinstance(shot, ScreenCapture)
    assert shot.png_bytes.startswith(b"\x89PNG")
    assert shot.virtual_rect == (0, 0, 640, 480)
    assert seen_cmds[-1][:2] == ["screencapture", "-x"] and len(seen_cmds[-1]) == 3
    assert list(work_tmp.iterdir()) == []


# ---------------------------------------------------------------------------
# 5. 根治回归：64 位 Win32 ctypes 签名、macOS value.vk=None / a(0) 键码、剪贴板保护与跨平台公共能力
# ---------------------------------------------------------------------------


def test_configure_win32_signatures_sets_64bit_safe_types():
    """验证 _configure_win32_signatures 为全部 Win32 API 显式声明 64 位安全的 restype 与 argtypes。"""

    class _FakeDllFunc:
        def __init__(self, name: str):
            self.name = name
            self.argtypes = None
            self.restype = None

    class _FakeDll:
        def __init__(self):
            self.funcs: dict[str, _FakeDllFunc] = {}

        def __getattr__(self, name: str) -> _FakeDllFunc:
            fn = self.funcs.setdefault(name, _FakeDllFunc(name))
            return fn

    user32 = _FakeDll()
    kernel32 = _FakeDll()
    _configure_win32_signatures(user32, kernel32)

    expected_user32 = {
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetWindowThreadProcessId": ([wintypes.HWND, wintypes.LPDWORD], wintypes.DWORD),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "SendInput": ([wintypes.UINT, ctypes.c_void_p, ctypes.c_int], wintypes.UINT),
        "OpenClipboard": ([wintypes.HWND], wintypes.BOOL),
        "CloseClipboard": ([], wintypes.BOOL),
        "EmptyClipboard": ([], wintypes.BOOL),
        "EnumClipboardFormats": ([wintypes.UINT], wintypes.UINT),
        "IsClipboardFormatAvailable": ([wintypes.UINT], wintypes.BOOL),
        "GetClipboardData": ([wintypes.UINT], wintypes.HANDLE),
        "SetClipboardData": ([wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
        "RegisterClipboardFormatW": ([wintypes.LPCWSTR], wintypes.UINT),
    }
    expected_kernel32 = {
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

    for name, (args, res) in expected_user32.items():
        fn = user32.funcs[name]
        assert fn.argtypes == args, name
        assert fn.restype is res, name

    for name, (args, res) in expected_kernel32.items():
        fn = kernel32.funcs[name]
        assert fn.argtypes == args, name
        assert fn.restype is res, name

    # 重点核验 64 位指针/尺寸类型，防止高 32 位截断崩溃
    assert kernel32.funcs["GlobalLock"].restype is ctypes.c_void_p
    assert kernel32.funcs["GlobalAlloc"].restype is wintypes.HGLOBAL
    assert ctypes.sizeof(kernel32.funcs["GlobalAlloc"].restype) == ctypes.sizeof(ctypes.c_void_p)
    assert kernel32.funcs["GlobalSize"].restype is ctypes.c_size_t
    assert VK_V == KEY_TABLE["v"].vk
    assert KVK_ANSI_V == KEY_TABLE["v"].macos_keycode


def test_extract_macos_key_info_handles_none_vk_and_zero_keycode_a():
    """根治回归：value.vk 为 None 不得抛 TypeError；macOS `a` 键码为 0 不得误判成 -1。"""

    class FakeVal:
        def __init__(self, vk):
            self.vk = vk

    class FakeModifierKey:
        def __init__(self, *, name: str, value=None, vk=None):
            self.name = name
            self.value = value
            if vk is not None:
                self.vk = vk

    mod_flag, kc = _extract_macos_key_info(FakeModifierKey(name="cmd", value=FakeVal(vk=None)))
    assert mod_flag == MACOS_MODIFIERS["super"]
    assert kc == -1

    mod_flag2, kc2 = _extract_macos_key_info(_FakeKey(name="cmd", vk=None))
    assert mod_flag2 == MACOS_MODIFIERS["super"]
    assert kc2 == -1

    # macOS kVK_ANSI_A == 0
    a_mod, a_kc = _extract_macos_key_info(_FakeKey(char="a", vk=0))
    assert a_mod == 0
    assert a_kc == 0
    a_mod2, a_kc2 = _extract_macos_key_info(_FakeKey(vk=0))
    assert a_mod2 == 0
    assert a_kc2 == 0

    # 端到端热键状态机验证：super(value.vk=None) + a(vk=0) 正常按住说话与松开停止
    events: list[str] = []
    listeners: list[_FakeListener] = []
    rig = MacOSHotkeyRig(
        HotkeyBindings(voice="super+a", screenshot="super+x", ocr="super+o", repeat="super+r"),
        on_voice_start=lambda: (events.append("start_a") or True),
        on_voice_stop=lambda: events.append("stop_a"),
        on_screenshot=lambda: events.append("shot"),
        listener_factory=lambda on_press, on_release: (
            listeners.append(_FakeListener(on_press, on_release)) or listeners[-1]
        ),
    )
    rig.start()
    lst = listeners[-1]
    lst.on_press(FakeModifierKey(name="cmd", value=FakeVal(vk=None)))
    lst.on_press(_FakeKey(char="a", vk=0))
    lst.on_release(_FakeKey(char="a", vk=0))
    lst.on_release(FakeModifierKey(name="cmd", value=FakeVal(vk=None)))
    assert events == ["start_a", "stop_a"]
    rig.stop()


def test_windows_and_macos_clipboard_protect_unsupported_and_failed_snapshots(tmp_path: Path):
    """根治回归：Win32 (CF_HDROP/打开失败) 与 macOS (furl/PDF/探测失败) 均不覆写清空用户剪贴板。"""

    # --- Windows CtypesWin32Api ---
    win_state = {"open_ok": True, "formats": [CF_HDROP], "emptied": 0}

    def _enum_fmt(prev: int) -> int:
        fmts = win_state["formats"]
        if not fmts:
            return 0
        if prev == 0:
            return fmts[0]
        idx = fmts.index(prev) + 1
        return fmts[idx] if idx < len(fmts) else 0

    user32 = _FakeWinDll(
        {
            "OpenClipboard": lambda _hwnd: 1 if win_state["open_ok"] else 0,
            "CloseClipboard": lambda: 1,
            "EmptyClipboard": lambda: (win_state.__setitem__("emptied", win_state["emptied"] + 1) or 1),
            "EnumClipboardFormats": _enum_fmt,
            "IsClipboardFormatAvailable": lambda fmt: 1 if fmt in win_state["formats"] else 0,
        }
    )
    kernel32 = _FakeWinDll({})
    win_api = CtypesWin32Api(user32=user32, kernel32=kernel32)

    snap_hdrop = win_api.snapshot_clipboard()
    assert snap_hdrop.kind == "unsupported"
    win_api.restore_clipboard(snap_hdrop)
    assert win_state["emptied"] == 0

    win_state["open_ok"] = False
    snap_fail = win_api.snapshot_clipboard()
    assert snap_fail.kind == "failed"
    win_state["open_ok"] = True
    win_api.restore_clipboard(snap_fail)
    assert win_state["emptied"] == 0

    win_state["formats"] = []
    snap_empty = win_api.snapshot_clipboard()
    assert snap_empty.kind == "empty"
    win_api.restore_clipboard(snap_empty)
    assert win_state["emptied"] == 1

    # --- macOS RealMacOSSystemApi ---
    pbcopy_writes: list[bytes] = []

    class _FakePbProc:
        def __init__(self):
            self.returncode = 0

        def communicate(self, payload=b"", timeout=None):
            del timeout
            pbcopy_writes.append(bytes(payload))
            return b"", b""

    mac_clip_info = {"rc": 0, "out": "«class furl», 64, «class utf8», 12"}

    def _fake_mac_run(cmd, **kw):
        del kw
        if cmd == ["osascript", "-e", "clipboard info"]:
            return subprocess.CompletedProcess(cmd, mac_clip_info["rc"], mac_clip_info["out"], "")
        if cmd == ["pbpaste"]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"err")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    mac_api = RealMacOSSystemApi(
        run=_fake_mac_run,
        popen=lambda *a, **kw: _FakePbProc(),
        which=lambda n: f"/usr/bin/{n}" if n in {"osascript", "pbcopy", "pbpaste"} else None,
    )
    snap_furl = mac_api.snapshot_clipboard(tmp_path)
    assert snap_furl.kind == "unsupported"
    mac_api.restore_clipboard(snap_furl, tmp_path)
    assert pbcopy_writes == []

    mac_clip_info["rc"] = 1
    snap_mac_fail = mac_api.snapshot_clipboard(tmp_path)
    assert snap_mac_fail.kind == "failed"
    mac_api.restore_clipboard(snap_mac_fail, tmp_path)
    assert pbcopy_writes == []

    mac_clip_info["rc"] = 0
    mac_clip_info["out"] = ""
    snap_mac_empty = mac_api.snapshot_clipboard(tmp_path)
    assert snap_mac_empty.kind == "empty"
    mac_api.restore_clipboard(snap_mac_empty, tmp_path)
    assert pbcopy_writes == [b""]


def test_platform_common_beep_and_png_dimensions(tmp_path: Path):
    """验证 _platform._common.play_beep 线程串行化与 png_dimensions 边界校验。"""
    bad = tmp_path / "not.png"
    bad.write_bytes(b"NOT_A_PNG_FILE_1234567890")
    with pytest.raises(ValueError):
        png_dimensions(bad)

    active = 0
    max_active = 0
    calls = 0
    guard = threading.Lock()

    class FakeSd:
        def stop(self):
            pass

        def play(self, _wave, _sr):
            nonlocal active, max_active, calls
            with guard:
                active += 1
                calls += 1
                max_active = max(max_active, active)
            time.sleep(0.015)
            with guard:
                active -= 1

        def wait(self):
            pass

    fake_sd = FakeSd()
    threads = [
        threading.Thread(
            target=common_play_beep,
            kwargs={"duration": 0.01, "background": False, "sounddevice_mod": fake_sd},
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls == 3
    assert max_active == 1


def test_sound_device_source_single_source_in_platform_common():
    """P2-6 回归：SoundDeviceSource 与 play_beep 单源位于 `_platform._common` 并被 `_linux.mic` 重导出。"""
    import _linux.mic as linux_mic
    from _platform._common import _AUDIO_LOCK, SoundDeviceSource, play_beep

    assert linux_mic.SoundDeviceSource is SoundDeviceSource
    assert linux_mic.play_beep is play_beep
    assert linux_mic._PORTAUDIO_LOCK is _AUDIO_LOCK

    received: list[bytes] = []
    lifecycle: list[str] = []

    class FakeStream:
        def __init__(self, **kw) -> None:
            self.cb = kw["callback"]
            lifecycle.append("init")

        def start(self) -> None:
            lifecycle.append("start")
            self.cb(b"\x01\x02\x03\x04", 2, None, None)

        def stop(self) -> None:
            lifecycle.append("stop")

        def close(self) -> None:
            lifecycle.append("close")

    class FakeSd:
        def RawInputStream(self, **kw):
            return FakeStream(**kw)

    src = SoundDeviceSource(sounddevice_mod=FakeSd())
    src.start(received.append)
    src.stop()
    assert received == [b"\x01\x02\x03\x04"]
    assert lifecycle == ["init", "start", "stop", "close"]


def test_capture_screen_and_crop_across_linux_windows_macos(tmp_path: Path):
    """三端全屏抓拍与唯一裁切口契约：X11/Win/Mac 优先 mss 抓 monitors[0]，Wayland 优先 Portal 整屏并回退 mss，
    macOS 兜底 screencapture -x 全屏；crop_png_region 覆盖 200%/150% HiDPI、负坐标多屏与越界钳制。"""
    from types import SimpleNamespace
    from PIL import Image
    from _platform.protocol import ScreenCapture, crop_png_region

    # 1. 构造 800x600 物理 PNG，其中 (200..400, 100..300) 为特征色 (18, 171, 86)
    img = Image.new("RGB", (800, 600), (15, 23, 42))
    for y in range(100, 300):
        for x in range(200, 400):
            img.putpixel((x, y), (18, 171, 86))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    full_png = buf.getvalue()

    # 1a. 200% HiDPI：逻辑虚拟桌面 (0,0,400,300) 上选 (100,50,100,100) → 精确裁出 200x200 绿色区域
    cropped_bytes = crop_png_region(full_png, (0, 0, 400, 300), (100, 50, 100, 100))
    with Image.open(io.BytesIO(cropped_bytes)) as cropped:
        assert cropped.size == (200, 200)
        assert cropped.getpixel((100, 100))[:3] == (18, 171, 86)

    # 1b. 负坐标多屏 150% HiDPI：逻辑 (-400,-100,600,200) 对应物理 900x300，选 (-300,-50,100,50) → 物理 (150,75,150,75)
    neg_img = Image.new("RGB", (900, 300), (10, 20, 30))
    for y in range(75, 150):
        for x in range(150, 300):
            neg_img.putpixel((x, y), (220, 90, 40))
    neg_buf = io.BytesIO()
    neg_img.save(neg_buf, format="PNG")
    neg_cropped = crop_png_region(neg_buf.getvalue(), (-400, -100, 600, 200), (-300, -50, 100, 50))
    with Image.open(io.BytesIO(neg_cropped)) as im_neg:
        assert im_neg.size == (150, 75)
        assert im_neg.getpixel((75, 35))[:3] == (220, 90, 40)

    # 1c. 越界选区（部分越界、完全越界右下与左上）均安全钳制且宽/高 >= 1；非法入参显式抛 ValueError
    with Image.open(io.BytesIO(crop_png_region(full_png, (0, 0, 400, 300), (350, 260, 100, 80)))) as im_edge:
        assert im_edge.size == (100, 80)
    with Image.open(io.BytesIO(crop_png_region(full_png, (0, 0, 400, 300), (500, 400, 50, 50)))) as im_br:
        assert im_br.size == (1, 1)
    with Image.open(io.BytesIO(crop_png_region(full_png, (0, 0, 400, 300), (-100, -80, 40, 20)))) as im_tl:
        assert im_tl.size == (1, 1)
    with pytest.raises(ValueError):
        crop_png_region(b"", (0, 0, 400, 300), (10, 10, 20, 20))
    with pytest.raises(ValueError):
        crop_png_region(full_png, (0, 0, 0, 300), (10, 10, 20, 20))
    with pytest.raises(ValueError):
        crop_png_region(full_png, (0, 0, 400, 300), (10, 10, 0, 20))

    # 2. Linux X11 / Windows / macOS 均通过 capture_screen_mss 抓取 monitors[0] 整屏
    grabbed_boxes: list[dict] = []

    def _make_mss_factory(mon0: dict):
        class _ScopedMss:
            monitors = [mon0]

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def grab(self, box):
                grabbed_boxes.append(dict(box))
                return SimpleNamespace(rgb=bytes([20, 40, 60]) * (16 * 12), size=(16, 12))

        return _ScopedMss

    linux_x11 = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "x11"},
        mss_factory=_make_mss_factory({"left": 0, "top": 0, "width": 3840, "height": 2160}),
        uinput_factory=None,
    )
    shot_x11 = linux_x11.capture_screen()
    assert shot_x11.png_bytes.startswith(b"\x89PNG")
    assert shot_x11.virtual_rect == (0, 0, 3840, 2160)
    assert grabbed_boxes[-1] == {"left": 0, "top": 0, "width": 3840, "height": 2160}

    win = WindowsPlatformAdapter(
        mss_factory=_make_mss_factory({"left": -2880, "top": 0, "width": 5760, "height": 1620})
    )
    shot_win = win.capture_screen()
    assert shot_win.png_bytes.startswith(b"\x89PNG")
    assert shot_win.virtual_rect == (-2880, 0, 5760, 1620)
    assert grabbed_boxes[-1] == {"left": -2880, "top": 0, "width": 5760, "height": 1620}

    mac_mss_api = RealMacOSSystemApi(
        mss_factory=_make_mss_factory({"left": 0, "top": 0, "width": 1512, "height": 982})
    )
    mac_mss = MacOSPlatformAdapter(macos_api=mac_mss_api, temp_dir=tmp_path / "mac_mss")
    shot_mac_mss = mac_mss.capture_screen()
    assert shot_mac_mss.png_bytes.startswith(b"\x89PNG")
    assert shot_mac_mss.virtual_rect == (0, 0, 1512, 982)
    assert grabbed_boxes[-1] == {"left": 0, "top": 0, "width": 1512, "height": 982}

    # 3. Linux Wayland：优先 Portal 整屏；Portal 失败或返回空字节回退 mss；双败抛出含 Portal 与 mss 的 RuntimeError
    linux_wl = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=lambda: ScreenCapture(png_bytes=full_png, virtual_rect=(0, 0, 800, 600)),
        uinput_factory=None,
    )
    shot_wl = linux_wl.capture_screen()
    assert shot_wl == ScreenCapture(png_bytes=full_png, virtual_rect=(0, 0, 800, 600))

    linux_wl_empty = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=lambda: ScreenCapture(png_bytes=b"", virtual_rect=(0, 0, 800, 600)),
        mss_factory=_make_mss_factory({"left": 0, "top": 0, "width": 1920, "height": 1080}),
        uinput_factory=None,
    )
    shot_wl_empty_fb = linux_wl_empty.capture_screen()
    assert shot_wl_empty_fb.png_bytes.startswith(b"\x89PNG")
    assert shot_wl_empty_fb.virtual_rect == (0, 0, 1920, 1080)

    linux_wl_fb = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=lambda: (_ for _ in ()).throw(RuntimeError("portal denied")),
        mss_factory=_make_mss_factory({"left": 0, "top": 0, "width": 3840, "height": 2160}),
        uinput_factory=None,
    )
    shot_wl_fb = linux_wl_fb.capture_screen()
    assert shot_wl_fb.png_bytes.startswith(b"\x89PNG")
    assert shot_wl_fb.virtual_rect == (0, 0, 3840, 2160)

    linux_wl_both_fail = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "wayland"},
        portal_capture_fn=lambda: (_ for _ in ()).throw(RuntimeError("portal denied")),
        mss_factory=lambda: (_ for _ in ()).throw(RuntimeError("mss unavailable")),
        uinput_factory=None,
    )
    with pytest.raises(RuntimeError, match="Portal.*mss"):
        linux_wl_both_fail.capture_screen()

    # 4. macOS mss 失败时回退 screencapture -x <tmp_png>（全屏抓取、清理临时文件、缺失命令显式报错）
    seen_cmds: list[list[str]] = []

    def fake_mac_run(cmd, **_kw):
        seen_cmds.append(list(cmd))
        Path(cmd[-1]).write_bytes(full_png)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def broken_mss():
        raise RuntimeError("mss denied")

    mac_fb_dir = tmp_path / "mac_fb"
    mac_api = RealMacOSSystemApi(
        run=fake_mac_run,
        which=lambda name: "/usr/sbin/screencapture" if name == "screencapture" else None,
        mss_factory=broken_mss,
    )
    mac = MacOSPlatformAdapter(macos_api=mac_api, temp_dir=mac_fb_dir)
    out_mac = mac.capture_screen()
    assert out_mac == ScreenCapture(png_bytes=full_png, virtual_rect=(0, 0, 800, 600))
    assert seen_cmds[-1][:2] == ["screencapture", "-x"] and len(seen_cmds[-1]) == 3
    assert list(mac_fb_dir.iterdir()) == []

    mac_no_bin = MacOSPlatformAdapter(
        macos_api=RealMacOSSystemApi(which=lambda _name: None, mss_factory=broken_mss),
        temp_dir=tmp_path / "mac_none",
    )
    with pytest.raises(RuntimeError, match="macOS 全屏抓屏不可用"):
        mac_no_bin.capture_screen()


def test_capture_screen_mss_monitors_validation_and_empty_png(monkeypatch):
    """capture_screen_mss 边界校验：monitors 缺失/非法尺寸或 PNG 编码空字节必须显式抛 RuntimeError。"""
    from types import SimpleNamespace
    import mss.tools

    from _platform.protocol import capture_screen_mss

    def _bad_mss(monitors_val):
        class _BadMss:
            monitors = monitors_val

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def grab(self, _box):
                return SimpleNamespace(rgb=b"\x00\x00\x00", size=(1, 1))

        return _BadMss

    for bad_monitors in (
        None,
        [],
        ["not-a-dict"],
        [{"left": 0, "top": 0, "width": 0, "height": 1080}],
        [{"left": 0, "top": 0, "width": 1920, "height": -1}],
    ):
        with pytest.raises(RuntimeError):
            capture_screen_mss(_bad_mss(bad_monitors))

    monkeypatch.setattr(mss.tools, "to_png", lambda _rgb, _size: b"")
    with pytest.raises(RuntimeError, match="空字节"):
        capture_screen_mss(_bad_mss([{"left": 0, "top": 0, "width": 1920, "height": 1080}]))


def test_pynput_hotkeys_modifier_and_own_binding_do_not_close_overlay():
    """Windows/macOS pynput：修饰键、自身四键与普通键都不关浮窗，只有 Esc 关闭（与 Linux evdev 一致）。"""
    listeners: list[_FakeListener] = []
    events: list[str] = []
    overlay_open = {"v": True}

    mac_bindings = HotkeyBindings(
        voice="super+alt+z",
        screenshot="super+shift+x",
        ocr="super+shift+o",
        repeat="super+alt+r",
    )
    rig = MacOSHotkeyRig(
        mac_bindings,
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
        overlay_active=lambda: overlay_open["v"],
        close_overlay=lambda: (overlay_open.__setitem__("v", False) or events.append("close_ov") or True),
        listener_factory=lambda on_press, on_release: (
            listeners.append(_FakeListener(on_press, on_release)) or listeners[-1]
        ),
    )
    rig.start()
    lst = listeners[-1]

    # 单独按下修饰键（含重复）绝不关闭活跃浮窗
    for mod in (
        _FakeKey(name="cmd", vk=0x37),
        _FakeKey(name="shift", vk=0x38),
        _FakeKey(name="alt", vk=0x3A),
    ):
        lst.on_press(mod)
        lst.on_release(mod)
    assert events == [] and overlay_open["v"] is True

    # 自身四个绑定组合键在浮窗活跃时直接派发动作，不先额外关闭浮窗
    lst.on_press(_FakeKey(name="cmd", vk=0x37))
    lst.on_press(_FakeKey(name="shift", vk=0x38))
    lst.on_press(_FakeKey(char="x", vk=0x07))
    lst.on_release(_FakeKey(char="x", vk=0x07))
    lst.on_press(_FakeKey(char="o", vk=0x1F))
    lst.on_release(_FakeKey(char="o", vk=0x1F))
    lst.on_release(_FakeKey(name="shift", vk=0x38))
    lst.on_press(_FakeKey(name="alt", vk=0x3A))
    lst.on_press(_FakeKey(char="r", vk=0x0F))
    lst.on_release(_FakeKey(char="r", vk=0x0F))
    lst.on_press(_FakeKey(char="z", vk=0x06))
    lst.on_release(_FakeKey(char="z", vk=0x06))
    lst.on_release(_FakeKey(name="alt", vk=0x3A))
    lst.on_release(_FakeKey(name="cmd", vk=0x37))
    assert events == ["shot", "ocr", "repeat", "v_start", "v_stop"]
    assert overlay_open["v"] is True

    # 普通键（如 'a'）不再关闭浮窗，只有 Esc 关闭
    lst.on_press(_FakeKey(char="a", vk=0x00))
    lst.on_release(_FakeKey(char="a", vk=0x00))
    assert overlay_open["v"] is True and "close_ov" not in events

    lst.on_press(_FakeKey(name="esc", vk=0x35))
    lst.on_release(_FakeKey(name="esc", vk=0x35))
    assert overlay_open["v"] is False and events[-1] == "close_ov"
    rig.stop()


def test_portal_screenshot_sync_uses_thread_local_main_context(monkeypatch, tmp_path: Path):
    """P2-1 回归：_portal_screenshot_sync 使用线程专属 GLib.MainContext.new() + push/pop，绝不迭代 default()。"""
    import sys
    import types
    from PIL import Image

    from _platform import linux as linux_mod

    shot_file = tmp_path / "portal_shot.png"
    Image.new("RGB", (40, 20), (10, 20, 30)).save(shot_file, format="PNG")

    trace: list[str] = []
    signal_cb_box: dict[str, object] = {}

    class FakeMainContext:
        def __init__(self) -> None:
            self.iter_count = 0

        def push_thread_default(self) -> None:
            trace.append("push")

        def pop_thread_default(self) -> None:
            trace.append("pop")

        def iteration(self, may_block: bool) -> bool:
            assert may_block is False
            trace.append("iter")
            self.iter_count += 1
            if self.iter_count == 1 and "cb" in signal_cb_box:
                cb = signal_cb_box["cb"]
                req_path = signal_cb_box["handle"]
                cb(
                    None,
                    None,
                    req_path,
                    "org.freedesktop.portal.Request",
                    "Response",
                    types.SimpleNamespace(unpack=lambda: (0, {"uri": f"file://{shot_file}"})),
                )
                return True
            return False

        @staticmethod
        def new() -> "FakeMainContext":
            trace.append("new_ctx")
            return FakeMainContext()

        @staticmethod
        def default():
            raise AssertionError("禁止在后台线程调用 GLib.MainContext.default()")

    class FakeConn:
        def get_unique_name(self) -> str:
            return ":1.42"

        def signal_subscribe(self, _sender, _iface, _sig, _path, _arg0, _flags, cb) -> int:
            trace.append("subscribe")
            signal_cb_box["cb"] = cb
            return 77

        def signal_unsubscribe(self, sub_id: int) -> None:
            assert sub_id == 77
            trace.append("unsubscribe")

        def call_sync(self, _bus, _path, _iface, _method, params, _rtype, _flags, _timeout, _cancel):
            trace.append("call_sync")
            token = params[1][1]["handle_token"][1]
            handle = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
            signal_cb_box["handle"] = handle
            return types.SimpleNamespace(unpack=lambda: (handle,))

    fake_glib = types.SimpleNamespace(
        MainContext=FakeMainContext,
        Variant=lambda sig, val: (sig, val),
        VariantType=types.SimpleNamespace(new=lambda sig: sig),
    )
    fake_gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=1),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        bus_get_sync=lambda _t, _c: FakeConn(),
    )
    fake_repo = types.ModuleType("gi.repository")
    fake_repo.Gio = fake_gio  # type: ignore[attr-defined]
    fake_repo.GLib = fake_glib  # type: ignore[attr-defined]
    fake_gi = types.ModuleType("gi")
    fake_gi.repository = fake_repo  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repo)
    monkeypatch.setattr(linux_mod, "register_portal_host_app", lambda *a, **kw: True)

    cap = linux_mod._portal_screenshot_sync(timeout_s=2.0)
    assert cap.png_bytes.startswith(b"\x89PNG")
    assert cap.virtual_rect == (0, 0, 40, 20)
    assert not shot_file.exists()  # 门户临时文件已清理
    # 验证顺序：new_ctx -> push -> subscribe -> call_sync -> iter -> unsubscribe -> pop
    assert trace.index("new_ctx") < trace.index("push") < trace.index("subscribe") < trace.index("call_sync")
    assert trace.index("iter") < trace.index("unsubscribe") < trace.index("pop")


# ---------------------------------------------------------------------------
# 桌面集成：自启命令与门户 app id 条目（_desktop.desktop_integration）
# ---------------------------------------------------------------------------
def test_autostart_uses_background_app_command_and_portal_entry_follows_xdg_order(tmp_path):
    import shlex

    from _desktop.desktop_integration import apply_autostart, ensure_portal_app_entry
    from _paths import AppPaths, app_command

    paths = AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r")
    calls: list[dict] = []

    class Adapter:
        def configure_autostart(self, *, enabled, command):
            calls.append({"enabled": enabled, "command": list(command)})

    apply_autostart(Adapter(), paths, False)
    assert calls == [{"enabled": False, "command": app_command(paths, background=True)}]
    assert calls[0]["command"][-1] == "--background"

    env = {"HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "user"),
           "XDG_DATA_DIRS": f"{tmp_path / 'sys1'}:{tmp_path / 'sys2'}"}
    system_entry = tmp_path / "sys2" / "applications" / "ollama-voice-input-daemon.desktop"
    system_entry.parent.mkdir(parents=True)
    system_entry.write_text("[Desktop Entry]\n", encoding="utf-8")
    assert ensure_portal_app_entry(paths, env) == system_entry  # 已有条目（如安装包放的）就用它，不另写
    assert not (tmp_path / "user" / "applications").exists()

    system_entry.unlink()
    written = ensure_portal_app_entry(paths, env)
    assert written == tmp_path / "user" / "applications" / "ollama-voice-input-daemon.desktop"
    text = written.read_text(encoding="utf-8")
    assert "NoDisplay=true" in text and f"Exec={shlex.join(app_command(paths, background=True))}" in text
    written.write_text("[Desktop Entry]\nExec=user-edited\n", encoding="utf-8")
    assert ensure_portal_app_entry(paths, env) == written and "user-edited" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# U10：三端 write_clipboard_png（Linux xclip / Windows CF_DIB / macOS osascript 公共路径）
# ---------------------------------------------------------------------------
def test_write_clipboard_png_across_linux_windows_and_macos(tmp_path: Path, monkeypatch):
    import os
    import re
    import struct
    from PIL import Image

    from _paths import default_app_paths

    buf = io.BytesIO()
    Image.new("RGBA", (6, 4), (12, 34, 56, 200)).save(buf, format="PNG")
    sample_png = buf.getvalue()

    # 1. Linux：经 SystemClipboard.write_binary 调 xclip -selection clipboard -t image/png 并喂原始 PNG 字节
    linux_calls: list[tuple[list[str], bytes]] = []
    linux_rc = {"code": 0}

    class _FakeXclipProc:
        def __init__(self, cmd: list[str]) -> None:
            self._cmd = list(cmd)
            self.returncode = linux_rc["code"]

        def communicate(self, payload: bytes = b"", timeout: float | None = None):
            del timeout
            linux_calls.append((self._cmd, bytes(payload)))
            return b"", b""

    linux_ok = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xclip" if name == "xclip" else None,
        popen=lambda cmd, **kw: _FakeXclipProc(cmd),
        uinput_factory=None,
    )
    assert linux_ok.write_clipboard_png(sample_png) is None
    assert linux_calls == [(["xclip", "-selection", "clipboard", "-t", "image/png"], sample_png)]

    linux_rc["code"] = 1
    err_rc = linux_ok.write_clipboard_png(sample_png)
    assert err_rc is not None and "失败" in err_rc
    assert linux_ok.write_clipboard_png(b"") is not None

    linux_missing = LinuxPlatformAdapter(
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda _n: None,
        uinput_factory=None,
    )
    err_missing = linux_missing.write_clipboard_png(sample_png)
    assert err_missing is not None and "xclip" in err_missing

    # 2. Windows：真实 Pillow 转 24 位 RGB DIB（BMP 去 14 字节头）→ 公开 Win32Api.write_clipboard_dib
    fake_win_api = _FakeWin32Api()
    assert not hasattr(fake_win_api, "_write_clipboard_bytes")
    assert WindowsPlatformAdapter(win32_api=fake_win_api).write_clipboard_png(sample_png) is None
    assert fake_win_api.clipboard.kind == "dib" and not fake_win_api.clipboard.dib_bytes.startswith(b"BM")

    win_clip: dict[str, object] = {"open_ok": True, "open_calls": 0, "buffers": {}, "next_h": 100, "set": None}

    def _global_alloc(_flags, size):
        h = int(win_clip["next_h"])
        win_clip["next_h"] = h + 1
        win_clip["buffers"][h] = ctypes.create_string_buffer(size)
        return h

    def _global_lock(h):
        return ctypes.addressof(win_clip["buffers"][int(h)])

    def _set_clip(fmt, h):
        win_clip["set"] = (int(fmt), bytes(win_clip["buffers"][int(h)].raw))
        return h

    user32 = _FakeWinDll(
        {
            "OpenClipboard": lambda _hwnd: (
                win_clip.__setitem__("open_calls", int(win_clip["open_calls"]) + 1)
                or (1 if win_clip["open_ok"] else 0)
            ),
            "CloseClipboard": lambda: 1,
            "EmptyClipboard": lambda: 1,
            "SetClipboardData": _set_clip,
        }
    )
    kernel32 = _FakeWinDll(
        {
            "GlobalAlloc": _global_alloc,
            "GlobalLock": _global_lock,
            "GlobalUnlock": lambda _h: 1,
            "GlobalFree": lambda _h: 0,
        }
    )
    win_adapter = WindowsPlatformAdapter(win32_api=CtypesWin32Api(user32=user32, kernel32=kernel32))
    assert win_adapter.write_clipboard_png(sample_png) is None
    assert win_clip["set"] is not None
    fmt_written, dib_bytes = win_clip["set"]  # type: ignore[misc]
    assert fmt_written == CF_DIB
    # DIB 以 40 字节 BITMAPINFOHEADER 起头，不含 14 字节 "BM" 文件头，宽高与 24bpp 与源 PNG 一致
    assert not dib_bytes.startswith(b"BM")
    hdr_size, width, height, planes, bpp = struct.unpack_from("<IiiHH", dib_bytes, 0)
    assert (hdr_size, width, height, planes, bpp) == (40, 6, 4, 1, 24)

    open_before = int(win_clip["open_calls"])
    bad_png_err = win_adapter.write_clipboard_png(b"not-a-png")
    assert bad_png_err is not None and int(win_clip["open_calls"]) == open_before
    assert win_adapter.write_clipboard_png(b"") is not None

    win_clip["open_ok"] = False
    open_fail_err = win_adapter.write_clipboard_png(sample_png)
    assert open_fail_err is not None and "剪贴板" in open_fail_err

    # 3. macOS：使用应用临时目录（绝不写 /tmp）+ osascript，并与 restore_clipboard 共用唯一实现
    default_mac = MacOSPlatformAdapter()
    assert default_mac._temp_dir == default_app_paths(os.environ, os_name="macos").temp_dir
    assert not str(default_mac._temp_dir).startswith("/tmp")

    mac_tmp = tmp_path / "mac_clip_tmp"
    mac_runs: list[tuple[list[str], Path, bytes]] = []
    mac_rc = {"code": 0, "stderr": ""}

    def _fake_mac_run(cmd, **kw):
        del kw
        m = re.search(r'POSIX file "([^"]+)"', cmd[-1])
        assert m is not None, cmd
        p = Path(m.group(1))
        assert p.is_relative_to(mac_tmp) and not str(p).startswith("/tmp")
        mac_runs.append((list(cmd), p, p.read_bytes()))
        return subprocess.CompletedProcess(cmd, mac_rc["code"], "", mac_rc["stderr"])

    mac_api = RealMacOSSystemApi(
        run=_fake_mac_run,
        which=lambda n: "/usr/bin/osascript" if n == "osascript" else None,
    )
    mac_adapter = MacOSPlatformAdapter(macos_api=mac_api, temp_dir=mac_tmp)
    assert mac_adapter.write_clipboard_png(sample_png) is None
    assert len(mac_runs) == 1
    cmd0, path0, written0 = mac_runs[0]
    assert cmd0[0] == "osascript" and "«class PNGf»" in cmd0[-1]
    assert written0 == sample_png
    assert not path0.exists() and list(mac_tmp.iterdir()) == []

    # restore_clipboard 必须复用 write_clipboard_png（不重复写第二份 osascript）
    delegated: list[tuple[bytes, Path]] = []
    orig_write_png = mac_api.write_clipboard_png

    def _spy_write_png(png: bytes, temp_dir: Path) -> str | None:
        delegated.append((png, temp_dir))
        return orig_write_png(png, temp_dir)

    monkeypatch.setattr(mac_api, "write_clipboard_png", _spy_write_png)
    mac_api.restore_clipboard(MacClipboardSnapshot(kind="png", png_bytes=sample_png), mac_tmp)
    assert delegated == [(sample_png, mac_tmp)]
    assert len(mac_runs) == 2 and list(mac_tmp.iterdir()) == []

    # 失败路径：osascript 非零退出仍必须清理临时文件；缺少 osascript 与空字节均报错
    mac_rc["code"] = 1
    mac_rc["stderr"] = "execution error: clipboard busy"
    mac_fail = mac_adapter.write_clipboard_png(sample_png)
    assert mac_fail is not None and "clipboard busy" in mac_fail
    assert list(mac_tmp.iterdir()) == []
    assert mac_adapter.write_clipboard_png(b"") is not None

    mac_no_osa = MacOSPlatformAdapter(
        macos_api=RealMacOSSystemApi(run=_fake_mac_run, which=lambda _n: None),
        temp_dir=mac_tmp,
    )
    no_osa_err = mac_no_osa.write_clipboard_png(sample_png)
    assert no_osa_err is not None and "osascript" in no_osa_err

    # 临时目录创建/写盘遇到 OSError 返回可读错误而非抛未捕获异常
    file_not_dir = tmp_path / "occupied_file"
    file_not_dir.write_text("not a dir", encoding="utf-8")
    mac_bad_tmp = MacOSPlatformAdapter(macos_api=mac_api, temp_dir=file_not_dir)
    os_err = mac_bad_tmp.write_clipboard_png(sample_png)
    assert os_err is not None and "临时文件" in os_err


def test_set_windows_app_user_model_id_ctypes_signature_and_guards(monkeypatch):
    from _platform import _win32_api as win32_mod

    with pytest.raises(ValueError):
        set_windows_app_user_model_id("   ")

    monkeypatch.setattr(win32_mod.os, "name", "posix")
    assert set_windows_app_user_model_id("io.github.jackche8.ollama-voice-input") is False

    seen: list[str] = []
    rc = {"value": 0}
    shell32 = _FakeWinDll(
        {"SetCurrentProcessExplicitAppUserModelID": lambda val: (seen.append(val) or rc["value"])}
    )
    assert set_windows_app_user_model_id("io.github.jackche8.ollama-voice-input", shell32=shell32) is True
    fn = shell32.SetCurrentProcessExplicitAppUserModelID
    assert fn.argtypes == [wintypes.LPCWSTR]
    assert fn.restype is ctypes.c_long
    assert seen == ["io.github.jackche8.ollama-voice-input"]

    rc["value"] = -2147467259  # E_FAIL
    assert set_windows_app_user_model_id("io.github.jackche8.ollama-voice-input", shell32=shell32) is False
