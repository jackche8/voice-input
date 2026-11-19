"""macOS 平台适配器：`screencapture`/`mss` + 前台 Bundle ID + `pbcopy`/`CGEvent Cmd+V` + 四键监听 + LaunchAgent。

所有系统调用统一经 `_platform._macos_api.MacOSSystemApi` 协议封装，严禁导入 `_linux.*`。
"""
from __future__ import annotations

import plistlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from _contracts import SetupCapability
from _hotkey import (
    KEY_TABLE,
    MACOS_MODIFIERS,
    HotkeyBindings,
    canonical_key,
    parse_hotkey_macos,
)
from _platform._common import (
    always_false,
    check_focus_and_self_guard,
    noop,
    play_beep as common_play_beep,
    probe_ocr_capabilities,
)
from _platform._macos_api import (
    KCG_EVENT_FLAG_MASK_COMMAND,
    KCG_HID_EVENT_TAP,
    KVK_ANSI_V,
    MacClipboardSnapshot,
    MacOSSystemApi,
    RealMacOSSystemApi,
)
from _platform._pynput_hotkeys import PynputHotkeyRig
from _platform.protocol import (
    FocusTarget,
    HotkeyRig,
    PasteResult,
    VirtualRect,
)

PASTE_RESTORE_DELAY_S = 0.55
DEFAULT_LAUNCH_AGENT_LABEL = "com.ollama-voice-input.daemon"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

MACOS_TERMINAL_BUNDLES = frozenset(
    {
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "net.kovidgoyal.kitty",
        "io.alacritty",
        "com.github.wez.wezterm",
    }
)

_MACOS_KEYCODE_TO_MOD = {
    0x37: MACOS_MODIFIERS["super"],  # kVK_Command
    0x36: MACOS_MODIFIERS["super"],  # kVK_RightCommand
    0x38: MACOS_MODIFIERS["shift"],  # kVK_Shift
    0x3C: MACOS_MODIFIERS["shift"],  # kVK_RightShift
    0x3A: MACOS_MODIFIERS["alt"],    # kVK_Option
    0x3D: MACOS_MODIFIERS["alt"],    # kVK_RightOption
    0x3B: MACOS_MODIFIERS["ctrl"],   # kVK_Control
    0x3E: MACOS_MODIFIERS["ctrl"],   # kVK_RightControl
}

_PYNPUT_NAME_TO_MACOS_MOD = {
    "ctrl": MACOS_MODIFIERS["ctrl"],
    "ctrl_l": MACOS_MODIFIERS["ctrl"],
    "ctrl_r": MACOS_MODIFIERS["ctrl"],
    "alt": MACOS_MODIFIERS["alt"],
    "alt_l": MACOS_MODIFIERS["alt"],
    "alt_r": MACOS_MODIFIERS["alt"],
    "alt_gr": MACOS_MODIFIERS["alt"],
    "option": MACOS_MODIFIERS["alt"],
    "shift": MACOS_MODIFIERS["shift"],
    "shift_l": MACOS_MODIFIERS["shift"],
    "shift_r": MACOS_MODIFIERS["shift"],
    "cmd": MACOS_MODIFIERS["super"],
    "cmd_l": MACOS_MODIFIERS["super"],
    "cmd_r": MACOS_MODIFIERS["super"],
}


def _extract_macos_key_info(key) -> tuple[int, int]:
    """从 pynput key 事件提取 `(cg_modifier_flag, macos_keycode)`。

    注意：macOS `a` 键的 `kVK_ANSI_A` 为 `0`，严禁使用 `raw_kc or -1`，必须显式判断 `is not None`。
    """
    name = getattr(key, "name", None)
    if isinstance(name, str):
        low = name.lower()
        if low in _PYNPUT_NAME_TO_MACOS_MOD:
            mod_flag = _PYNPUT_NAME_TO_MACOS_MOD[low]
            val = getattr(key, "value", None)
            raw_kc = getattr(val, "vk", None) if val is not None else getattr(key, "vk", None)
            if raw_kc is None and val is not None:
                raw_kc = getattr(key, "vk", None)
            kc = int(raw_kc) if raw_kc is not None else -1
            return mod_flag, kc

    raw_vk = getattr(key, "vk", None)
    if raw_vk is None:
        val = getattr(key, "value", None)
        raw_vk = getattr(val, "vk", None) if val is not None else None

    if raw_vk is not None:
        kc = int(raw_vk)
        if kc in _MACOS_KEYCODE_TO_MOD:
            return _MACOS_KEYCODE_TO_MOD[kc], kc
        return 0, kc

    ch = getattr(key, "char", None)
    token = ch if isinstance(ch, str) and ch else (name if isinstance(name, str) else "")
    if token:
        entry = KEY_TABLE.get(canonical_key(token))
        if entry is not None:
            return 0, entry.macos_keycode
    return 0, -1


class MacOSHotkeyRig(PynputHotkeyRig):
    """基于 `_hotkey.parse_hotkey_macos` + `PynputHotkeyRig` 的 macOS 四键监听器。"""

    def __init__(
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
        listener_factory: Callable[..., object] | None = None,
    ) -> None:
        super().__init__(
            bindings,
            backend_name="macos-pynput",
            hotkey_parser=parse_hotkey_macos,
            key_extractor=_extract_macos_key_info,
            is_valid_code=lambda kc: kc >= 0,
            on_voice_start=on_voice_start,
            on_voice_stop=on_voice_stop,
            on_screenshot=on_screenshot,
            on_ocr=on_ocr,
            on_repeat=on_repeat,
            overlay_active=overlay_active,
            close_overlay=close_overlay,
            listener_factory=listener_factory,
        )


class MacOSPlatformAdapter:
    os_name = "macos"
    session_type = "macos"

    def __init__(
        self,
        *,
        macos_api: MacOSSystemApi | None = None,
        mss_factory=None,
        listener_factory: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        temp_dir: Path | None = None,
        ocr_languages: Callable[[], list[str]] | None = None,
    ) -> None:
        self._macos_api = macos_api
        self._mss_factory = mss_factory
        self._listener_factory = listener_factory
        self._sleep = sleep
        self._temp_dir = Path(temp_dir) if temp_dir is not None else (PROJECT_ROOT / ".pp" / "tmp" / "macos")
        self._ocr_languages = ocr_languages
        self._inject_lock = threading.Lock()

    def _api(self) -> MacOSSystemApi:
        if self._macos_api is None:
            self._macos_api = RealMacOSSystemApi(mss_factory=self._mss_factory)
        return self._macos_api

    def capture_region(
        self,
        region: VirtualRect,
        *,
        virtual_rect: VirtualRect | None = None,
    ) -> bytes:
        return self._api().capture_region(region, self._temp_dir, virtual_rect=virtual_rect)

    def current_focus(self) -> FocusTarget:
        api = self._api()
        bundle_id, pid, name = api.frontmost_app()
        if not bundle_id and not pid:
            return FocusTarget(focus_id="", app_id="", is_terminal=False, is_self=False)
        low_bid = bundle_id.lower()
        is_self = bool(
            (pid and pid == api.get_current_pid())
            or "ollama-voice-input" in low_bid
            or "ollama-voice-input" in (name or "").lower()
        )
        is_terminal = low_bid in MACOS_TERMINAL_BUNDLES
        focus_id = f"{bundle_id}:{pid}" if bundle_id and pid else (str(pid) if pid else bundle_id)
        return FocusTarget(
            focus_id=focus_id,
            app_id=bundle_id or name,
            is_terminal=is_terminal,
            is_self=is_self,
        )

    def read_clipboard_text(self) -> str:
        return self._api().read_clipboard_text()

    def write_clipboard_text(self, text: str) -> str | None:
        return self._api().write_clipboard_text(text)

    def inject_text(
        self,
        text: str,
        *,
        target: FocusTarget | None = None,
        terminal: bool | None = None,
    ) -> PasteResult:
        del terminal  # macOS 终端与常规 GUI 应用统一使用 Cmd+V 粘贴
        blocked = check_focus_and_self_guard(target)
        if blocked is not None:
            return blocked

        with self._inject_lock:
            api = self._api()
            live = self.current_focus()
            blocked = check_focus_and_self_guard(target, live)
            if blocked is not None:
                return blocked

            snap = api.snapshot_clipboard(self._temp_dir)
            err = api.write_clipboard_text(text)
            if err is not None:
                return PasteResult(False, "clipboard", err, uncertain=False)

            try:
                sent = api.post_cmd_v()
            except Exception as exc:
                self._restore_if_unchanged(api, snap, text)
                return PasteResult(False, "cgevent_cmd_v", f"CGEvent 发送 Cmd+V 异常: {exc}", uncertain=True)

            if not sent:
                self._restore_if_unchanged(api, snap, text)
                return PasteResult(
                    False,
                    "cgevent_cmd_v",
                    "无法发送 Cmd+V（请在系统设置 -> 隐私与安全性 -> 辅助功能中授权）",
                    uncertain=False,
                )

            self._sleep(PASTE_RESTORE_DELAY_S)
            self._restore_if_unchanged(api, snap, text)
            return PasteResult(True, "cgevent_cmd_v", "已通过 CGEvent Cmd+V 粘贴并恢复原剪贴板")

    def _restore_if_unchanged(self, api: MacOSSystemApi, snap: MacClipboardSnapshot, injected: str) -> None:
        if snap.kind in ("unsupported", "failed"):
            return
        try:
            if api.read_clipboard_text() == injected:
                api.restore_clipboard(snap, self._temp_dir)
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
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
    ) -> HotkeyRig:
        return MacOSHotkeyRig(
            bindings,
            on_voice_start=on_voice_start,
            on_voice_stop=on_voice_stop,
            on_screenshot=on_screenshot,
            on_ocr=on_ocr,
            on_repeat=on_repeat,
            overlay_active=overlay_active,
            close_overlay=close_overlay,
            listener_factory=self._listener_factory,
        )

    @staticmethod
    def launch_agent_path(
        label: str = DEFAULT_LAUNCH_AGENT_LABEL,
        *,
        home: Path | None = None,
    ) -> Path:
        root = Path.home() if home is None else Path(home)
        return root / "Library" / "LaunchAgents" / f"{label}.plist"

    @staticmethod
    def render_launch_agent_plist(
        command: Sequence[str],
        *,
        label: str = DEFAULT_LAUNCH_AGENT_LABEL,
        workdir: Path | None = None,
        log_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "Label": label,
            "ProgramArguments": [str(part) for part in command],
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        if workdir is not None:
            payload["WorkingDirectory"] = str(workdir)
        if log_dir is not None:
            payload["StandardOutPath"] = str(Path(log_dir) / "daemon.out.log")
            payload["StandardErrorPath"] = str(Path(log_dir) / "daemon.err.log")
        if env:
            payload["EnvironmentVariables"] = {str(k): str(v) for k, v in env.items()}
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")

    def configure_autostart(
        self,
        *,
        enabled: bool,
        command: Sequence[str],
        label: str = DEFAULT_LAUNCH_AGENT_LABEL,
        workdir: Path | None = None,
        log_dir: Path | None = None,
        home: Path | None = None,
    ) -> Path:
        plist_path = self.launch_agent_path(label, home=home)
        if not enabled:
            plist_path.unlink(missing_ok=True)
            return plist_path
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        xml_text = self.render_launch_agent_plist(
            command, label=label, workdir=workdir, log_dir=log_dir
        )
        plist_path.write_text(xml_text, encoding="utf-8")
        return plist_path

    def capabilities(self) -> list[SetupCapability]:
        api = self._api()
        ax_ok = api.is_accessibility_trusted()
        sr_ok = api.is_screen_recording_allowed()
        caps = [
            SetupCapability(
                id="macos_accessibility",
                label="macOS 辅助功能权限 (Accessibility)",
                available=ax_ok,
                required=True,
                detail="已授权全局热键与 CGEvent 键入"
                if ax_ok
                else "未授权：请前往 系统设置 -> 隐私与安全性 -> 辅助功能 开启权限",
            ),
            SetupCapability(
                id="macos_screen_recording",
                label="macOS 屏幕录制权限 (Screen Recording)",
                available=sr_ok,
                required=True,
                detail="已授权屏幕截图识别"
                if sr_ok
                else "未授权：请前往 系统设置 -> 隐私与安全性 -> 录屏与系统录音 开启权限",
            ),
            SetupCapability(
                id="clipboard",
                label="macOS 剪贴板 (pbcopy/pbpaste)",
                available=True,
                required=True,
                detail="pbcopy / pbpaste 就绪",
            ),
        ]
        caps.extend(probe_ocr_capabilities(self._ocr_languages, missing_prefix="tesseract 缺少"))
        return caps

    def play_beep(self, freq: float = 440, duration: float = 0.1, volume: float = 0.3) -> None:
        common_play_beep(freq=freq, duration=duration, volume=volume)

    def close(self) -> None:
        return None


__all__ = [
    "DEFAULT_LAUNCH_AGENT_LABEL",
    "KCG_EVENT_FLAG_MASK_COMMAND",
    "KCG_HID_EVENT_TAP",
    "KVK_ANSI_V",
    "MACOS_TERMINAL_BUNDLES",
    "PASTE_RESTORE_DELAY_S",
    "MacClipboardSnapshot",
    "MacOSHotkeyRig",
    "MacOSPlatformAdapter",
    "MacOSSystemApi",
    "RealMacOSSystemApi",
    "_extract_macos_key_info",
]
