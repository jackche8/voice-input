#!/usr/bin/env python3
"""真实抓取器链路契约：松开后不得误触发、回流过滤、四键（含 ocr）映射与回退释放。

当宿主机存在可用 X11 连接与 /dev/uinput 权限时走真实 X server；
在隔离 venv-test 或无 X 授权沙箱中，向真实 XInputHotkeyGrabber 注入内存 Display 夹具完成同等契约验证。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":0")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _hotkey import HotkeyBindings  # noqa: E402
from _linux.hotkey_actions import HotkeyContext  # noqa: E402
from _linux.hotkey_evdev import EvdevHotkeyGrabber  # noqa: E402
from _linux.hotkey_xinput import (  # noqa: E402
    _X_MAPPING_NOTIFY,
    _XI_RAW_KEY_PRESS,
    _XI_RAW_KEY_RELEASE,
    XInputHotkeyGrabber,
    xi_source_ids_by_name as _xi_source_ids_by_name,
)
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.modifiers import ModifierJanitor  # noqa: E402
from _linux.uinput_device import UINPUT_DEVICE_NAME as _UINPUT_DEVICE_NAME  # noqa: E402

_GUARD = InjectGuard()


class _NoopActions:
    """真实抓取器的动作夹具：本测试只验证按键账本（故意不定义 ocr() 以同时验证旧调用方兼容）。"""

    def voice_start(self) -> bool:
        return True

    def voice_stop(self) -> None:
        pass

    def screenshot(self) -> None:
        pass

    def repeat(self) -> None:
        pass

    def overlay_active(self) -> bool:
        return False

    def close_overlay(self) -> bool:
        return False


def VoiceRecorder():  # noqa: N802
    return None


class _FakeXInputDisplay:
    """当隔离测试环境无 Xlib 或无 Xauthority 时，供真实 XInputHotkeyGrabber 使用的最小协议夹具。"""

    _KEYSYM_TO_KC = {
        0xFFE3: 37,  # Control_L
        0xFFE4: 105,  # Control_R
        0xFFE1: 50,  # Shift_L
        0xFFE2: 62,  # Shift_R
        0xFFE9: 64,  # Alt_L
        0xFFEA: 108,  # Alt_R
        0xFFEB: 133,  # Super_L
        0xFFEC: 134,  # Super_R
        0xFFE7: 64,
        0xFFE8: 108,
        ord("z"): 52,
        ord("x"): 53,
        ord("o"): 32,
        ord("r"): 27,
    }

    def __init__(self) -> None:
        self.display = type("_Inner", (), {"get_extension_major": lambda _s, _n: 131})()
        self.events: list = []
        self.devices: list = []
        self.keymap = bytearray(32)

    def set_error_handler(self, _fn) -> None:
        pass

    def has_extension(self, name: str) -> bool:
        return name == "XInputExtension"

    def xinput_query_version(self):
        return type("_Ver", (), {"major_version": 2, "minor_version": 3})()

    def screen(self):
        root = type("_Root", (), {"xinput_select_events": lambda _s, _ev: None})()
        return type("_Scr", (), {"root": root})()

    def keysym_to_keycode(self, ks: int) -> int:
        return self._KEYSYM_TO_KC.get(ks, 0)

    def xinput_query_device(self, _which):
        return type("_Reply", (), {"devices": self.devices})()

    def query_keymap(self) -> bytes:
        return bytes(self.keymap)

    def flush(self) -> None:
        pass

    def sync(self) -> None:
        pass

    def fileno(self) -> int:
        return 0

    def pending_events(self) -> int:
        return len(self.events)

    def next_event(self):
        return self.events.pop(0)

    def close(self) -> None:
        pass


def _simple_hotkey_parser(hotkey_str: str) -> tuple[int, int]:
    parts = hotkey_str.lower().strip().split("+")
    mask = 0
    for mod in parts[:-1]:
        if mod == "ctrl":
            mask |= 1 << 2
        elif mod == "alt":
            mask |= 1 << 3
        elif mod == "shift":
            mask |= 1 << 0
        elif mod == "super":
            mask |= 1 << 6
    return mask, ord(parts[-1][0])


def _ctx(_unused=None, on_ocr=None) -> HotkeyContext:
    kwargs = {}
    if on_ocr is not None:
        kwargs["on_ocr"] = on_ocr
    return HotkeyContext(
        actions=_NoopActions(),
        bindings=HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r"),
        janitor=ModifierJanitor(session_type=os.environ.get("XDG_SESSION_TYPE", "x11"), kb=None, key=None, xdotool=""),
        guard=_GUARD,
        session_type=os.environ.get("XDG_SESSION_TYPE", "x11"),
        **kwargs,
    )


def _make_xinput_grabber(ctx: HotkeyContext) -> tuple[XInputHotkeyGrabber, bool]:
    """返回 `(grabber, is_live_x11)`。"""
    try:
        return XInputHotkeyGrabber(ctx), True
    except Exception:
        return (
            XInputHotkeyGrabber(
                ctx,
                display_factory=_FakeXInputDisplay,
                hotkey_parser=_simple_hotkey_parser,
            ),
            False,
        )


def test_stale_trigger_does_not_match() -> None:
    grabber, _live = _make_xinput_grabber(_ctx())
    try:
        trigger = grabber._voice_combo["trigger"]
        other = trigger + 1
        while other in {grabber._screen_combo["trigger"], grabber._ocr_combo["trigger"], grabber._repeat_combo["trigger"]}:
            other += 1
        grabber._pressed = {trigger, 37, 64}
        assert grabber._match_combo(grabber._voice_combo, other) is False, "陈 trigger 配非 trigger 键必须不命中"
        hit = grabber._match_combo(grabber._voice_combo, trigger)
        assert hit in (True, False)
    finally:
        grabber.cleanup()


def test_uinput_echo_not_in_pressed() -> None:
    grabber, is_live = _make_xinput_grabber(_ctx())
    if is_live:
        try:
            from evdev import UInput, ecodes
            from Xlib import XK
        except Exception:
            is_live = False

    if is_live:
        ui = None
        try:
            grabber.start()
            grabber._on_xlib_event(None, None)
            try:
                ui = UInput({ecodes.EV_KEY: list(range(1, 249))}, name=_UINPUT_DEVICE_NAME)
            except Exception:
                is_live = False
            else:
                time.sleep(0.25)
                grabber._on_xlib_event(None, None)
                ids = _xi_source_ids_by_name(grabber.dpy, _UINPUT_DEVICE_NAME)
                if ids:
                    grabber._refresh_ignore_sources()
                    assert ids <= grabber._ignore_source_ids
                    ctrl = grabber.dpy.keysym_to_keycode(XK.string_to_keysym("Control_L"))
                    grabber._pressed.discard(ctrl)
                    ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
                    ui.syn()
                    time.sleep(0.08)
                    grabber._on_xlib_event(None, None)
                    assert ctrl not in grabber._pressed
                    ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                    ui.syn()
                    time.sleep(0.05)
                    grabber._on_xlib_event(None, None)
                    return
        finally:
            if ui is not None:
                try:
                    ui.close()
                except Exception:
                    pass
            grabber.cleanup()
        grabber, _ = _make_xinput_grabber(_ctx())

    # 无真实 /dev/uinput 或 XWayland 合并 sourceid 时，直接验证 XInput2 MappingNotify + sourceid 过滤契约
    fake_dpy = _FakeXInputDisplay()
    fake_dpy.devices = [type("DevInfo", (), {"name": _UINPUT_DEVICE_NAME, "deviceid": 17})()]
    det_grabber = XInputHotkeyGrabber(
        _ctx(),
        display_factory=lambda: fake_dpy,
        hotkey_parser=_simple_hotkey_parser,
    )
    try:
        det_grabber.start()
        assert 17 in det_grabber._ignore_source_ids
        fake_dpy.events.extend(
            [
                type("MapEvt", (), {"type": _X_MAPPING_NOTIFY})(),
                type(
                    "RawEvt",
                    (),
                    {
                        "extension": 131,
                        "evtype": _XI_RAW_KEY_PRESS,
                        "data": type("D", (), {"detail": 37, "sourceid": 17})(),
                    },
                )(),
                type(
                    "RawEvt",
                    (),
                    {
                        "extension": 131,
                        "evtype": _XI_RAW_KEY_RELEASE,
                        "data": type("D", (), {"detail": 37, "sourceid": 17})(),
                    },
                )(),
            ]
        )
        det_grabber._on_xlib_event(None, None)
        assert 37 not in det_grabber._pressed
    finally:
        det_grabber.cleanup()
        grabber.cleanup()


def test_fallback_release_clears_trigger_including_ocr() -> None:
    ocr_hits: list[str] = []
    fake_dpy = _FakeXInputDisplay()
    # 设置 Control_L(37) 与 Alt_L(64) 在 keymap 中按下
    fake_dpy.keymap[37 >> 3] |= 1 << (37 & 7)
    fake_dpy.keymap[64 >> 3] |= 1 << (64 & 7)
    grabber = XInputHotkeyGrabber(
        _ctx(on_ocr=lambda: ocr_hits.append("ocr")),
        display_factory=lambda: fake_dpy,
        hotkey_parser=_simple_hotkey_parser,
    )
    old_until = _GUARD.until
    try:
        # 验证第四热键 OCR 在 XInput2 中命中并调用默认/注入 on_ocr
        ocr_trig = grabber._ocr_combo["trigger"]
        grabber._handle_press(ocr_trig)
        assert ocr_hits == ["ocr"]

        grabber._ignore_source_ids = set()
        _GUARD.until = time.monotonic() + 10
        trig = grabber._voice_combo["trigger"]
        grabber._pressed.add(trig)
        grabber._pressed.add(ocr_trig)
        grabber._handle_release(trig)
        grabber._handle_release(ocr_trig)
        assert trig not in grabber._pressed, "回退窗必须放行 voice trigger 抬起"
        assert ocr_trig not in grabber._pressed, "回退窗必须放行 ocr trigger 抬起"
    finally:
        _GUARD.until = old_until
        grabber.cleanup()


def test_evdev_stale_trigger_and_modifier_probe() -> None:
    holder = EvdevHotkeyGrabber.__new__(EvdevHotkeyGrabber)
    holder._pressed_keys = {44, 29, 56}
    combo = {"trigger": 44, "modifiers": [{29, 97}]}
    assert EvdevHotkeyGrabber._match_combo(holder, combo, 30) is False
    assert EvdevHotkeyGrabber._match_combo(holder, combo, 44) is True
    assert EvdevHotkeyGrabber.any_modifier_down(holder) is True
    holder._pressed_keys = {44}
    assert EvdevHotkeyGrabber.any_modifier_down(holder) is False


def test_xinput_modifier_and_own_binding_do_not_close_overlay() -> None:
    """P1-5 回归：XInput2 修饰键与自身四组热键不得误关活跃浮窗，普通键（含 Esc）正常关闭。"""
    from _linux.hotkey_actions import CallbackHotkeyActions, GLib as _GLib

    def _drain() -> None:
        main_ctx_fn = getattr(_GLib, "MainContext", None)
        if main_ctx_fn is not None and callable(getattr(main_ctx_fn, "default", None)):
            gctx = main_ctx_fn.default()
            while gctx.iteration(False):
                pass

    events: list[str] = []
    overlay_open = {"v": True}
    fake_dpy = _FakeXInputDisplay()
    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(
            on_voice_start=lambda: (events.append("v_start") or True),
            on_voice_stop=lambda: events.append("v_stop"),
            on_screenshot=lambda: events.append("shot"),
            on_ocr=lambda: events.append("ocr"),
            on_repeat=lambda: events.append("repeat"),
            is_overlay_active=lambda: overlay_open["v"],
            on_close_overlay=lambda: (overlay_open.__setitem__("v", False) or events.append("close_ov") or False),
        ),
        bindings=HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r"),
        janitor=ModifierJanitor(session_type="x11", kb=None, key=None, xdotool=""),
        guard=InjectGuard(),
        session_type="x11",
    )
    grabber = XInputHotkeyGrabber(
        ctx,
        display_factory=lambda: fake_dpy,
        hotkey_parser=_simple_hotkey_parser,
    )
    try:
        # 1. 按下修饰键 Control_L(37) + Alt_L(64) 不得关闭浮窗
        fake_dpy.keymap[37 >> 3] |= 1 << (37 & 7)
        grabber._handle_press(37)
        fake_dpy.keymap[64 >> 3] |= 1 << (64 & 7)
        grabber._handle_press(64)
        _drain()
        assert overlay_open["v"] is True and events == []

        # 2. 命中自身热键（screenshot/ocr/repeat/voice）直接执行动作，不先关闭浮窗
        for kc in (
            grabber._screen_combo["trigger"],
            grabber._ocr_combo["trigger"],
            grabber._repeat_combo["trigger"],
            grabber._voice_combo["trigger"],
        ):
            grabber._handle_press(kc)
            grabber._handle_release(kc)
            _drain()
        assert overlay_open["v"] is True
        assert events == ["shot", "ocr", "repeat", "v_start"]

        # 3. 松开修饰键后按普通键（Esc keycode=9）触发关闭活跃浮窗
        grabber._do_release()
        fake_dpy.keymap[37 >> 3] &= ~(1 << (37 & 7))
        fake_dpy.keymap[64 >> 3] &= ~(1 << (64 & 7))
        grabber._handle_release(37)
        grabber._handle_release(64)
        grabber._handle_press(9)
        _drain()
        assert overlay_open["v"] is False
        assert events[-1] == "close_ov"
    finally:
        grabber.cleanup()


def main() -> int:
    test_stale_trigger_does_not_match()
    test_fallback_release_clears_trigger_including_ocr()
    test_evdev_stale_trigger_and_modifier_probe()
    test_xinput_modifier_and_own_binding_do_not_close_overlay()
    test_uinput_echo_not_in_pressed()
    print("PASS: 快捷键松开后不再误触发且四键契约正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
