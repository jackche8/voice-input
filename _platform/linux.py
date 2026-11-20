"""Linux 平台适配器：组装根治后的 `_linux/*` 能力与 `mss` / Wayland Portal 区域抓屏。"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from _contracts import SetupCapability
from _hotkey import HotkeyBindings
from _linux.clipboard import SystemClipboard
from _linux.conflicts import ConflictScan, scan_hotkey_conflicts
from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyContext
from _linux.hotkey_manager import HotkeyManager
from _linux.ibus import IBusCommitter
from _linux.inject_guard import InjectGuard
from _linux.modifiers import ModifierJanitor
from _linux.paste_backend import FocusedWindowInfo, LinuxPasteBackend, probe_x11_window_info
from _linux.portal_dbus import (
    GLOBAL_SHORTCUTS_IFACE,
    PORTAL_NAME,
    PORTAL_PATH,
    SCREENSHOT_IFACE,
    portal_interface_version,
    register_portal_host_app,
)
from _linux.uinput_device import open_uinput
from _platform._common import (
    always_false,
    check_focus_and_self_guard,
    noop,
    play_beep as common_play_beep,
    png_dimensions,
    probe_ocr_capabilities,
)
from _platform.protocol import (
    FocusTarget,
    HotkeyRig,
    PasteResult,
    ScreenCapture,
    VirtualRect,
    capture_region_mss,
    crop_png_region,
)


def _portal_screenshot_sync(timeout_s: float = 15.0) -> ScreenCapture:
    """通过 Gio D-Bus 同步调用 Wayland `org.freedesktop.portal.Screenshot` 并清理门户残留图片。"""
    from gi.repository import Gio, GLib

    register_portal_host_app()
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    unique = conn.get_unique_name() or ""
    sender_slug = unique.lstrip(":").replace(".", "_")
    token = f"ovi_shot_{os.getpid()}_{int(time.time() * 1000) % 100000000}"
    expected_req_path = f"/org/freedesktop/portal/desktop/request/{sender_slug}/{token}"

    result_box: dict[str, object] = {}

    def _on_signal(_conn, _sender, object_path, _iface, _signal, params) -> None:
        if expected_req_path and object_path != expected_req_path and result_box.get("handle") not in (None, object_path):
            return
        response, details = params.unpack()
        result_box["response"] = int(response)
        result_box["details"] = details

    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    sub_id: int | None = None
    try:
        sub_id = conn.signal_subscribe(
            PORTAL_NAME,
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_signal,
        )
        opts = {
            "handle_token": GLib.Variant("s", token),
            "interactive": GLib.Variant("b", False),
            "modal": GLib.Variant("b", False),
        }
        reply = conn.call_sync(
            PORTAL_NAME,
            PORTAL_PATH,
            SCREENSHOT_IFACE,
            "Screenshot",
            GLib.Variant("(sa{sv})", ("", opts)),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        handle = reply.unpack()[0]
        result_box["handle"] = str(handle)

        deadline = time.monotonic() + timeout_s
        while "response" not in result_box and time.monotonic() < deadline:
            if not ctx.iteration(False):
                time.sleep(0.01)
    finally:
        if sub_id is not None:
            conn.signal_unsubscribe(sub_id)
        ctx.pop_thread_default()

    if "response" not in result_box:
        raise RuntimeError("Wayland Portal Screenshot 响应超时")
    if result_box["response"] != 0:
        raise RuntimeError(f"Wayland Portal Screenshot 被拒绝 (response={result_box['response']})")
    details = result_box.get("details") or {}
    uri = details.get("uri") if isinstance(details, dict) else None
    if not uri:
        raise RuntimeError("Wayland Portal Screenshot 未返回 URI")
    parsed = urllib.parse.urlparse(str(uri))
    file_path = Path(urllib.parse.unquote(parsed.path or str(uri)))
    if not file_path.is_file():
        raise RuntimeError(f"Wayland Portal Screenshot 文件不存在: {file_path}")
    try:
        png_bytes = file_path.read_bytes()
        width, height = png_dimensions(str(file_path))
    finally:
        file_path.unlink(missing_ok=True)
    return ScreenCapture(png_bytes=png_bytes, virtual_rect=(0, 0, width, height))


class LinuxHotkeyRig:
    """包装 `HotkeyManager` 满足跨平台 `HotkeyRig` 契约。"""

    def __init__(self, manager: HotkeyManager) -> None:
        self._manager = manager

    @property
    def backend_name(self) -> str:
        return self._manager.backend_name

    def start(self) -> None:
        self._manager.start()

    def stop(self) -> None:
        self._manager.cleanup()

    def reset_voice(self) -> None:
        self._manager.reset_voice()


AUTOSTART_ENTRY = "ollama-voice-input.desktop"


class LinuxPlatformAdapter:
    os_name = "linux"

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen=subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        mss_factory=None,
        portal_capture_fn: Callable[[], ScreenCapture] | None = None,
        portal_version_fn: Callable[[str], int | None] = portal_interface_version,
        window_probe: Callable[[], FocusedWindowInfo] | None = None,
        clipboard: SystemClipboard | None = None,
        ibus: IBusCommitter | None = None,
        janitor: ModifierJanitor | None = None,
        uinput_factory: Callable[[], object] | None = open_uinput,
        hotkey_manager_cls=HotkeyManager,
        ocr_languages: Callable[[], list[str]] | None = None,
    ) -> None:
        self._env = os.environ if environ is None else environ
        self.session_type = (self._env.get("XDG_SESSION_TYPE") or "x11").strip().lower() or "x11"
        self._which = which
        self._run = run
        self._popen = popen
        self._sleep = sleep
        self._mss_factory = mss_factory
        self._portal_capture_fn = _portal_screenshot_sync if portal_capture_fn is None else portal_capture_fn
        self._portal_version_fn = portal_version_fn
        self._window_probe = probe_x11_window_info if window_probe is None else window_probe
        self._clipboard = clipboard or SystemClipboard(
            which=which, environ=self._env, popen=popen, run=run
        )
        self._ibus = ibus
        self._janitor = janitor
        self._uinput = None
        self._uinput_factory = uinput_factory
        self._guard = InjectGuard()
        self._paste_backend: LinuxPasteBackend | None = None
        self._hotkey_manager_cls = hotkey_manager_cls
        self._ocr_languages = ocr_languages

    def _ensure_janitor(self) -> ModifierJanitor:
        if self._janitor is not None:
            return self._janitor
        if self._uinput is None and self._uinput_factory is not None:
            try:
                self._uinput = self._uinput_factory()
            except Exception:
                self._uinput = None
        self._janitor = ModifierJanitor(
            session_type=self.session_type,
            uinput=self._uinput,
            xdotool=self._which("xdotool"),
        )
        return self._janitor

    def _ensure_paste_backend(self) -> LinuxPasteBackend:
        if self._paste_backend is None:
            ibus = self._ibus if self._ibus is not None else IBusCommitter.connect(
                environ=self._env, which=self._which, run=self._run
            )
            self._ibus = ibus
            self._paste_backend = LinuxPasteBackend(
                session_type=self.session_type,
                guard=self._guard,
                janitor=self._ensure_janitor(),
                clipboard=self._clipboard,
                ibus=ibus,
                run=self._run,
                which=self._which,
                sleep=self._sleep,
                window_probe=self._window_probe,
            )
        return self._paste_backend

    def capture_region(
        self,
        region: VirtualRect,
        *,
        virtual_rect: VirtualRect | None = None,
    ) -> bytes:
        """框选后直接按目标区域截屏：非 Wayland 直走 `mss` 区域抓取；Wayland 优先 Portal 裁切，失败回退 `mss`。"""
        _rx, _ry, rw, rh = (int(v) for v in region)
        if rw <= 0 or rh <= 0:
            raise ValueError(f"选区宽高必须大于 0，收到 {region!r}")
        if virtual_rect is not None:
            _vx, _vy, vw, vh = (int(v) for v in virtual_rect)
            if vw <= 0 or vh <= 0:
                raise ValueError(f"virtual_rect 宽高必须大于 0，收到 {virtual_rect!r}")

        if self.session_type != "wayland":
            return capture_region_mss(region, self._mss_factory, virtual_rect=virtual_rect)

        portal_err: Exception | None = None
        try:
            shot = self._portal_capture_fn()
            ref_rect = virtual_rect if virtual_rect is not None else shot.virtual_rect
            return crop_png_region(shot.png_bytes, ref_rect, region)
        except Exception as exc:
            portal_err = exc
        try:
            return capture_region_mss(region, self._mss_factory, virtual_rect=virtual_rect)
        except Exception as mss_exc:
            raise RuntimeError(
                f"Wayland 区域抓屏失败：Portal ({portal_err}) 与 mss 回退 ({mss_exc}) 均不可用"
            ) from mss_exc

    def current_focus(self) -> FocusTarget:
        info = self._window_probe()
        app_id = info.wm_class[-1] if info.wm_class else ""
        return FocusTarget(
            focus_id=str(info.window_id) if info.window_id else "",
            app_id=app_id,
            is_terminal=info.is_terminal,
            is_self=info.is_self,
        )

    def read_clipboard_text(self) -> str:
        return self._clipboard.read()

    def write_clipboard_text(self, text: str) -> str | None:
        return self._clipboard.write(text)

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
        live = self.current_focus()
        blocked = check_focus_and_self_guard(target, live)
        if blocked is not None:
            return blocked
        use_terminal = (
            terminal
            if terminal is not None
            else bool((target and target.is_terminal) or live.is_terminal)
        )
        ev = self._ensure_paste_backend().deliver(text, terminal=use_terminal)
        uncertain = not ev.proved_output and not ev.proved_none
        return PasteResult(ok=ev.proved_output, method=ev.method, detail=ev.detail, uncertain=uncertain)

    def create_hotkey_rig(
        self,
        bindings: HotkeyBindings,
        *,
        on_voice_start: Callable[[], bool],
        on_voice_stop: Callable[[], None],
        on_screenshot: Callable[[], None],
        on_ocr: Callable[[], None] = noop,
        on_repeat: Callable[[], None] = noop,
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
        on_error: Callable[[str | None], None],
    ) -> HotkeyRig:
        """组件失败（evdev 不可读、实际绑定解析不了、事件源意外退出）经 on_error 上报，恢复时 on_error(None)。"""
        actions = CallbackHotkeyActions(
            on_voice_start=on_voice_start,
            on_voice_stop=on_voice_stop,
            on_screenshot=on_screenshot,
            on_ocr=on_ocr,
            on_repeat=on_repeat,
            is_overlay_active=overlay_active,
            on_close_overlay=close_overlay,
        )
        ctx = HotkeyContext(
            actions=actions,
            bindings=bindings,
            janitor=self._ensure_janitor(),
            guard=self._guard,
            session_type=self.session_type,
        )
        return LinuxHotkeyRig(self._hotkey_manager_cls(ctx, on_error=on_error))

    def capabilities(self, bindings: HotkeyBindings | None = None) -> list[SetupCapability]:
        xclip_path = self._which("xclip") or self._which("xsel") or (
            self._which("wl-copy") if self.session_type == "wayland" else None
        )
        caps = [
            SetupCapability(
                id="clipboard",
                label="系统剪贴板工具",
                available=xclip_path is not None,
                required=True,
                detail=xclip_path or "未找到 xclip / xsel",
            )
        ]
        if self.session_type == "wayland":
            shot_ver = self._portal_version_fn(SCREENSHOT_IFACE)
            hk_ver = self._portal_version_fn(GLOBAL_SHORTCUTS_IFACE)
            caps.append(
                SetupCapability(
                    id="portal_screenshot",
                    label="Wayland 截图 Portal",
                    available=shot_ver is not None,
                    required=True,
                    detail=f"version {shot_ver}" if shot_ver is not None else "org.freedesktop.portal.Screenshot 不可用",
                )
            )
            caps.append(
                SetupCapability(
                    id="portal_shortcuts",
                    label="Wayland 全局快捷键 Portal",
                    available=hk_ver is not None,
                    required=True,
                    detail=f"version {hk_ver}" if hk_ver is not None else "org.freedesktop.portal.GlobalShortcuts 不可用",
                )
            )
        else:
            caps.append(
                SetupCapability(
                    id="x11_capture",
                    label="X11 多屏抓屏 (mss)",
                    available=True,
                    required=True,
                    detail="mss 虚拟桌面抓屏就绪",
                )
            )
        if bindings is not None:
            scan: ConflictScan = scan_hotkey_conflicts(bindings, run=self._run, which=self._which)
            caps.append(
                SetupCapability(
                    id="hotkey_conflicts",
                    label="快捷键冲突检查",
                    available=not scan.conflicts and not scan.unread,
                    required=False,
                    detail="；".join(scan.conflicts) if scan.conflicts else ("；".join(scan.unread) if scan.unread else "未发现冲突"),
                )
            )
        caps.extend(probe_ocr_capabilities(self._ocr_languages, missing_prefix="缺少"))
        return caps

    @staticmethod
    def render_autostart_desktop(
        command: Sequence[str],
        *,
        workdir: Path | None = None,
        name: str = "Ollama Voice Input",
    ) -> str:
        exec_line = " ".join(shlex.quote(str(part)) for part in command)
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={name}",
            f"Exec={exec_line}",
            "X-GNOME-Autostart-enabled=true",
            "Terminal=false",
        ]
        if workdir is not None:
            lines.append(f"Path={workdir}")
        return "\n".join(lines) + "\n"

    def configure_autostart(self, *, enabled: bool, command: Sequence[str], config_home: Path | None = None) -> None:
        """当前用户的 XDG 自启条目 `$XDG_CONFIG_HOME/autostart/ollama-voice-input.desktop`（缺省 ~/.config）。"""
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = config_home or (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config")
        entry = base / "autostart" / AUTOSTART_ENTRY
        if not enabled:
            entry.unlink(missing_ok=True)
            return
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(self.render_autostart_desktop(command), encoding="utf-8")

    def play_beep(self, freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
        common_play_beep(freq=freq, duration=duration, volume=volume)

    def close(self) -> None:
        if self._janitor is not None:
            try:
                self._janitor.panic_release_all()
            except Exception:
                pass
        if self._uinput is not None:
            try:
                self._uinput.close()
            except Exception:
                pass
            self._uinput = None
