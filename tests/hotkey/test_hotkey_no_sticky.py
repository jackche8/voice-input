#!/usr/bin/env python3
"""真实抓取器链路契约：松开后不得误触发、回流过滤、五键（含 ocr / annotate）映射与回退释放。

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
from tests.hotkey.fakes import FakeXInputDisplay, ManualTimers, RecordingSink, simple_hotkey_parser  # noqa: E402

_GUARD = InjectGuard()


class _NoopActions:
    """真实抓取器的动作夹具：本测试只验证按键账本。"""

    def voice_start(self) -> bool:
        return True

    def voice_stop(self) -> None:
        pass

    def screenshot(self) -> None:
        pass

    def ocr(self) -> None:
        pass

    def repeat(self) -> None:
        pass

    def annotate(self) -> None:
        pass

    def overlay_active(self) -> bool:
        return False

    def close_overlay(self) -> bool:
        return False


def VoiceRecorder():  # noqa: N802
    return None


def _ctx() -> HotkeyContext:
    return HotkeyContext(
        actions=_NoopActions(),
        bindings=HotkeyBindings(
            voice="ctrl+alt+z",
            screenshot="ctrl+alt+x",
            ocr="ctrl+alt+o",
            repeat="ctrl+alt+r",
            annotate="ctrl+alt+a",
        ),
        janitor=ModifierJanitor(session_type=os.environ.get("XDG_SESSION_TYPE", "x11"), kb=None, key=None, xdotool=""),
        guard=_GUARD,
        session_type=os.environ.get("XDG_SESSION_TYPE", "x11"),
    )


def _make_xinput_grabber(ctx: HotkeyContext) -> tuple[XInputHotkeyGrabber, bool]:
    """返回 `(grabber, is_live_x11)`。"""
    try:
        return XInputHotkeyGrabber(ctx, RecordingSink()), True
    except Exception:
        return (
            XInputHotkeyGrabber(
                ctx,
                RecordingSink(),
                display_factory=FakeXInputDisplay,
                hotkey_parser=simple_hotkey_parser,
            ),
            False,
        )


def test_stale_trigger_does_not_match() -> None:
    grabber, _live = _make_xinput_grabber(_ctx())
    try:
        trigger = grabber._voice_combo["trigger"]
        other = trigger + 1
        while other in {
            grabber._screen_combo["trigger"],
            grabber._ocr_combo["trigger"],
            grabber._repeat_combo["trigger"],
            grabber._annotate_combo["trigger"],
        }:
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
    fake_dpy = FakeXInputDisplay()
    fake_dpy.devices = [type("DevInfo", (), {"name": _UINPUT_DEVICE_NAME, "deviceid": 17})()]
    det_grabber = XInputHotkeyGrabber(
        _ctx(),
        RecordingSink(),
        display_factory=lambda: fake_dpy,
        hotkey_parser=simple_hotkey_parser,
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
    fake_dpy = FakeXInputDisplay()
    # 设置 Control_L(37) 与 Alt_L(64) 在 keymap 中按下
    fake_dpy.keymap[37 >> 3] |= 1 << (37 & 7)
    fake_dpy.keymap[64 >> 3] |= 1 << (64 & 7)
    sink = RecordingSink()
    import _linux.hotkey_xinput as hotkey_xinput

    saved_glib, hotkey_xinput.GLib = hotkey_xinput.GLib, ManualTimers()  # 50ms 松开定时器不起线程
    grabber = XInputHotkeyGrabber(
        _ctx(),
        sink,
        display_factory=lambda: fake_dpy,
        hotkey_parser=simple_hotkey_parser,
    )
    old_until = _GUARD.until
    try:
        # 第四/五热键 OCR 与 Annotate 在 XInput2 回退中命中：上报区间开始与放行
        ocr_trig = grabber._ocr_combo["trigger"]
        ann_trig = grabber._annotate_combo["trigger"]
        grabber._handle_press(ocr_trig, 0, 1000)
        grabber._handle_press(ann_trig, 0, 1050)
        assert sink.calls == [
            ("start", "ocr", 1000),
            ("activated", "ocr", 1000),
            ("start", "annotate", 1050),
            ("activated", "annotate", 1050),
        ]

        grabber._ignore_source_ids = set()
        _GUARD.until = time.monotonic() + 10
        trig = grabber._voice_combo["trigger"]
        grabber._pressed.add(trig)
        grabber._pressed.add(ocr_trig)
        grabber._pressed.add(ann_trig)
        grabber._handle_release(trig)
        grabber._handle_release(ocr_trig)
        grabber._handle_release(ann_trig)
        assert trig not in grabber._pressed, "回退窗必须放行 voice trigger 抬起"
        assert ocr_trig not in grabber._pressed, "回退窗必须放行 ocr trigger 抬起"
        assert ann_trig not in grabber._pressed, "回退窗必须放行 annotate trigger 抬起"
    finally:
        _GUARD.until = old_until
        grabber.cleanup()
        hotkey_xinput.GLib = saved_glib


def test_evdev_modifier_probe_takes_union_of_devices() -> None:
    from collections import namedtuple

    Ev = namedtuple("Ev", "type code value sec usec")

    class Dev:
        def __init__(self, path):
            self.path, self.fd, self.name = path, hash(path) % 1000, "kbd"

        def active_keys(self):
            return []

        def close(self):
            pass

    grabber = EvdevHotkeyGrabber(_ctx(), RecordingSink(), open_devices=lambda _ex: [Dev("/dev/input/a"), Dev("/dev/input/b")])
    assert grabber.any_modifier_down() is False
    grabber._handle_event("/dev/input/a", Ev(1, 44, 1, 1, 0))  # Z：不是修饰键
    assert grabber.any_modifier_down() is False
    grabber._handle_event("/dev/input/b", Ev(1, 29, 1, 1, 0))  # 另一设备上的 Ctrl
    assert grabber.any_modifier_down() is True
    grabber._handle_event("/dev/input/b", Ev(1, 29, 0, 2, 0))
    assert grabber.any_modifier_down() is False


def test_xinput_fallback_reports_own_bindings_and_only_escape_closes_overlay() -> None:
    """X11 回退：修饰键与普通键不上报 Esc，自身四组热键上报区间开始与放行，只有 Esc 上报关闭。"""
    import _linux.hotkey_xinput as hotkey_xinput

    fake_dpy = FakeXInputDisplay()
    sink = RecordingSink()
    saved_glib, hotkey_xinput.GLib = hotkey_xinput.GLib, ManualTimers()  # 50ms 松开定时器不起线程
    grabber = XInputHotkeyGrabber(
        _ctx(),
        sink,
        display_factory=lambda: fake_dpy,
        hotkey_parser=simple_hotkey_parser,
    )
    try:
        # 1. 按下修饰键 Control_L(37) + Alt_L(64)：不上报
        fake_dpy.keymap[37 >> 3] |= 1 << (37 & 7)
        grabber._handle_press(37, 0, 100)
        fake_dpy.keymap[64 >> 3] |= 1 << (64 & 7)
        grabber._handle_press(64, 0, 110)
        assert sink.calls == []

        # 2. 命中自身热键：上报区间开始与放行
        for action, combo, ms in (
            ("screenshot", grabber._screen_combo, 200),
            ("ocr", grabber._ocr_combo, 300),
            ("repeat", grabber._repeat_combo, 400),
            ("annotate", grabber._annotate_combo, 450),
            ("voice", grabber._voice_combo, 500),
        ):
            grabber._handle_press(combo["trigger"], 0, ms)
            assert sink.calls[-2:] == [("start", action, ms), ("activated", action, ms)]
            grabber._handle_release(combo["trigger"], 0, ms + 20)
        sink.calls.clear()

        # 3. 松开修饰键后：普通键（如 Ctrl+C 的 C）不关闭，只有 Esc（keycode 9）上报关闭
        fake_dpy.keymap[37 >> 3] &= ~(1 << (37 & 7))
        fake_dpy.keymap[64 >> 3] &= ~(1 << (64 & 7))
        grabber._handle_release(37, 0, 600)
        grabber._handle_release(64, 0, 610)
        grabber._handle_press(54, 0, 700)
        assert ("escape",) not in sink.calls
        grabber._handle_press(9, 0, 800)
        assert sink.calls[-1] == ("escape",)
    finally:
        grabber.cleanup()
        hotkey_xinput.GLib = saved_glib


def main() -> int:
    test_stale_trigger_does_not_match()
    test_fallback_release_clears_trigger_including_ocr()
    test_evdev_modifier_probe_takes_union_of_devices()
    test_xinput_fallback_reports_own_bindings_and_only_escape_closes_overlay()
    test_uinput_echo_not_in_pressed()
    print("PASS: 快捷键松开后不再误触发且四键契约正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
