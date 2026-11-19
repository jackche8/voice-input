#!/usr/bin/env python3
"""纯函数、四键映射与 Portal GlobalShortcuts 全生命周期契约测试（不争抢真实热键）。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("DISPLAY", "")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from _hotkey import HotkeyBindings, normalize_accel, to_portal_trigger  # noqa: E402
from _linux.conflicts import scan_hotkey_conflicts  # noqa: E402
from _linux.geometry import selection_crop_box  # noqa: E402
from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyActionMixin, HotkeyContext  # noqa: E402
from _linux.hotkey_evdev import EvdevHotkeyGrabber  # noqa: E402
from _linux.hotkey_portal import (  # noqa: E402
    DISCRETE_DEBOUNCE_S,
    DISCRETE_STALE_S,
    PortalGlobalShortcutsGrabber,
    action_from_shortcut_id,
    normalize_portal_trigger,
    portal_shortcut_id,
    portal_trigger_matches,
)
from _linux.hotkey_x11 import _X_KEY_PRESS, _X_KEY_RELEASE, HotkeyGrabber  # noqa: E402
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.portal_dbus import (  # noqa: E402
    GLOBAL_SHORTCUTS_IFACE,
    HOST_REGISTRY_IFACE,
    PORTAL_NAME,
    register_portal_host_app,
)

DEFAULTS = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r")


def _drain_glib() -> None:
    from _linux.hotkey_actions import GLib as _GLib

    main_ctx_fn = getattr(_GLib, "MainContext", None)
    if main_ctx_fn is not None and callable(getattr(main_ctx_fn, "default", None)):
        gctx = main_ctx_fn.default()
        while gctx.iteration(False):
            pass


def test_portal_trigger_format_and_fingerprint_id() -> None:
    assert to_portal_trigger("ctrl+alt+z") == "CTRL+ALT+Z"
    assert to_portal_trigger("ctrl+alt+x") == "CTRL+ALT+X"
    assert to_portal_trigger("ctrl+alt+o") == "CTRL+ALT+O"
    assert to_portal_trigger("ctrl+grave") == "CTRL+grave"
    assert portal_shortcut_id("screenshot", "ctrl+alt+x") == "screenshot_ctrl_alt_x"
    assert portal_shortcut_id("screenshot", "Ctrl+Shift+S") == "screenshot_ctrl_shift_s"
    assert portal_shortcut_id("ocr", "ctrl+alt+o") == "ocr_ctrl_alt_o"
    assert action_from_shortcut_id("screenshot_ctrl_shift_s") == "screenshot"
    assert action_from_shortcut_id("ocr_ctrl_alt_o") == "ocr"
    assert action_from_shortcut_id("voice") == "voice"
    assert portal_trigger_matches("CTRL+ALT+X", "ctrl+alt+x") is True
    assert portal_trigger_matches("按 <Control><Alt>z", "ctrl+alt+z") is True
    assert portal_trigger_matches("LOGO+O", "super+o") is True
    assert portal_trigger_matches("CTRL+ALT+X", "ctrl+shift+s") is False

    # 根治回归：修饰键任意排列顺序与中英文引导词前缀均不得产生假阳性不匹配
    assert normalize_portal_trigger("<Shift><Alt>x") == "alt+shift+x"
    assert portal_trigger_matches("<Shift><Alt>x", "alt+shift+x") is True
    assert normalize_portal_trigger("<Primary><Shift><Alt>z") == "ctrl+alt+shift+z"
    assert portal_trigger_matches("<Primary><Shift><Alt>z", "ctrl+alt+shift+z") is True
    assert normalize_portal_trigger("SHIFT+CTRL+S") == "ctrl+shift+s"
    assert portal_trigger_matches("SHIFT+CTRL+S", "ctrl+shift+s") is True
    assert normalize_portal_trigger("Press Ctrl+Alt+Z") == "ctrl+alt+z"
    assert portal_trigger_matches("Press Ctrl+Alt+Z", "ctrl+alt+z") is True
    assert portal_trigger_matches("按 Ctrl+Alt+Z", "ctrl+alt+z") is True
    assert not hasattr(PortalGlobalShortcutsGrabber, "_register_host_app")


def test_accel_normalize() -> None:
    assert normalize_accel("<Primary><Alt>d") == "ctrl+alt+d"
    assert normalize_accel("<Control><Shift>z") == "ctrl+shift+z"
    assert normalize_accel("<Super>d") == "super+d"


def test_selection_crop_box() -> None:
    assert selection_crop_box(0, 0, 1.0, 1.0, 200, 200, 400, 200) == (200, 200, 600, 400)
    assert selection_crop_box(0, 0, 2.0, 2.0, 100, 100, 200, 100) == (200, 200, 600, 400)
    assert selection_crop_box(1920, 0, 1.0, 1.0, 10, 20, 30, 40) == (1930, 20, 1960, 60)


def test_parse_shortcuts_description() -> None:
    grabber = PortalGlobalShortcutsGrabber.__new__(PortalGlobalShortcutsGrabber)
    raw = [
        ("voice", {"trigger_description": "按 <Control><Alt>z"}),
        ("screenshot", {"trigger": "CTRL+ALT+X"}),
        ("ocr", {"trigger": "CTRL+ALT+O"}),
        ("repeat", {"preferred_trigger": "CTRL+ALT+R"}),
    ]
    parsed = grabber._parse_shortcuts(raw)
    assert parsed["voice"] == "按 <Control><Alt>z"
    assert parsed["screenshot"] == "CTRL+ALT+X"
    assert parsed["ocr"] == "CTRL+ALT+O"
    assert parsed["repeat"] == "CTRL+ALT+R"


class _DummyRecorder:
    """旧版 HotkeyActions 夹具（故意不含 ocr 方法，验证默认空回调向后兼容）。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def voice_start(self) -> bool:
        self.starts += 1
        return True

    def voice_stop(self) -> None:
        self.stops += 1

    def screenshot(self) -> None:
        pass

    def repeat(self) -> None:
        pass

    def overlay_active(self) -> bool:
        return False

    def close_overlay(self) -> bool:
        return False


class _DummyActions(HotkeyActionMixin):
    _release_modifiers_on_stop = False

    def __init__(self) -> None:
        self.recorder = _DummyRecorder()
        self._init_hotkey_actions(
            HotkeyContext(
                actions=self.recorder,
                bindings=DEFAULTS,
                janitor=None,
                guard=InjectGuard(),
                session_type="x11",
            )
        )


def test_action_mixin_voice_start_stop_and_default_ocr_noop() -> None:
    actions = _DummyActions()
    actions._on_voice_press()
    actions._on_voice_press()
    assert actions.voice_active is True
    assert actions.recorder.starts == 1
    assert actions._on_ocr() is False  # 旧调用方无 ocr() 时安全 no-op，不抛异常
    actions._do_release()
    actions._do_release()
    assert actions.voice_active is False
    assert actions.recorder.stops == 1


def test_portal_four_hotkeys_rebind_mismatch_and_bus_restart_recovery() -> None:
    """验证 Portal 四键指纹 ID、改键不匹配自动调 ConfigureShortcuts、以及 Portal 总线重启自动重注册宿主。"""

    class FakeMatch:
        def __init__(self):
            self.removed = False

        def remove(self):
            self.removed = True

    class FakePortalIface:
        def __init__(self, state):
            self.state = state

        def connect_to_signal(self, name, cb):
            self.state["signals"][name] = cb
            return FakeMatch()

        def CreateSession(self, _opts):
            self.state["create_sessions"] += 1
            return f"/req/sess/{self.state['create_sessions']}"

        def BindShortcuts(self, session_handle, shortcuts, _parent, _opts):
            self.state["bound_calls"].append((str(session_handle), list(shortcuts)))
            return f"/req/bind/{len(self.state['bound_calls'])}"

        def ConfigureShortcuts(self, session_handle, _parent, _opts):
            self.state["configure_calls"].append(str(session_handle))

        def Register(self, app_id, options):
            self.state["host_registers"].append((str(app_id), dict(options)))

        def Close(self):
            self.state["closed_sessions"] += 1

    class FakeReqIface:
        def __init__(self, path, state):
            self.path = path
            self.state = state

        def connect_to_signal(self, name, cb):
            assert name == "Response"
            self.state["requests"][self.path] = cb
            return FakeMatch()

    class FakeBus:
        def __init__(self, state):
            self.state = state
            self.owner_cb = None

        def get_object(self, bus_name, obj_path):
            return (bus_name, str(obj_path))

        def watch_name_owner(self, name, cb):
            assert name == PORTAL_NAME
            self.owner_cb = cb
            cb(":1.10")
            return "watch-token"

        def unwatch_name_owner(self, _tok):
            self.owner_cb = None

    class FakeDbusMod:
        def __init__(self, state):
            self.state = state

        def String(self, s):
            return str(s)

        def ObjectPath(self, p):
            return str(p)

        def Dictionary(self, d, signature=None):
            del signature
            return dict(d)

        def Array(self, items, signature=None):
            del signature
            return list(items)

        def Interface(self, obj, iface_name):
            _bus_name, path = obj
            if iface_name == "org.freedesktop.portal.Request":
                return FakeReqIface(path, self.state)
            if iface_name in {GLOBAL_SHORTCUTS_IFACE, HOST_REGISTRY_IFACE, "org.freedesktop.portal.Session"}:
                return FakePortalIface(self.state)
            raise AssertionError(f"unexpected interface {iface_name}")

    state = {
        "signals": {},
        "requests": {},
        "create_sessions": 0,
        "bound_calls": [],
        "configure_calls": [],
        "host_registers": [],
        "closed_sessions": 0,
    }
    events: list[str] = []
    actions = CallbackHotkeyActions(
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
    )
    custom_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+shift+s", ocr="ctrl+alt+o", repeat="ctrl+alt+r"
    )
    ctx = HotkeyContext(
        actions=actions,
        bindings=custom_bindings,
        janitor=None,
        guard=InjectGuard(),
        session_type="wayland",
    )
    bus = FakeBus(state)
    bound_count = 0
    failed_count = 0

    def on_bound():
        nonlocal bound_count
        bound_count += 1

    def on_failed():
        nonlocal failed_count
        failed_count += 1

    grabber = PortalGlobalShortcutsGrabber(
        ctx,
        on_bound=on_bound,
        on_failed=on_failed,
        dbus_mod=FakeDbusMod(state),
        bus=bus,
        version_probe=lambda _iface: 2,
    )
    grabber.start()
    assert state["create_sessions"] == 1
    state["requests"]["/req/sess/1"](0, {"session_handle": "/session/1"})
    assert len(state["bound_calls"]) == 1
    _sess, shortcuts = state["bound_calls"][0]
    sids = [sid for sid, _props in shortcuts]
    assert sids == [
        "voice_ctrl_alt_z",
        "screenshot_ctrl_shift_s",
        "ocr_ctrl_alt_o",
        "repeat_ctrl_alt_r",
    ]

    # 模拟 GNOME 返回旧触发器 CTRL+ALT+X（与配置的 ctrl+shift+s 不一致）→ 自动触发 ConfigureShortcuts
    state["requests"]["/req/bind/1"](
        0,
        {
            "shortcuts": [
                ("voice_ctrl_alt_z", {"trigger": "CTRL+ALT+Z"}),
                ("screenshot_ctrl_shift_s", {"trigger": "CTRL+ALT+X"}),
                ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
                ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
            ]
        },
    )
    assert bound_count == 1
    assert "screenshot" in grabber.mismatched_triggers
    assert state["configure_calls"] == ["/session/1"]

    # 根治回归：ShortcutsChanged 信号到来时同步重算 mismatched_triggers（修复后清空，新冲突即时反映，且不死循环弹 ConfigureShortcuts）
    state["signals"]["ShortcutsChanged"](
        "/session/1",
        [
            ("voice_ctrl_alt_z", {"trigger": "Press Ctrl+Alt+Z"}),
            ("screenshot_ctrl_shift_s", {"trigger": "SHIFT+CTRL+S"}),
            ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
            ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
        ],
    )
    assert grabber.mismatched_triggers == {}
    assert state["configure_calls"] == ["/session/1"]

    state["signals"]["ShortcutsChanged"](
        "/session/1",
        [
            ("voice_ctrl_alt_z", {"trigger": "CTRL+ALT+Z"}),
            ("screenshot_ctrl_shift_s", {"trigger": "CTRL+ALT+X"}),
            ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
            ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
        ],
    )
    assert "screenshot" in grabber.mismatched_triggers
    assert state["configure_calls"] == ["/session/1"]

    # 验证四条热键信号派发（含按下/松开）
    state["signals"]["Activated"]("/session/1", "voice_ctrl_alt_z", 0, {})
    state["signals"]["Deactivated"]("/session/1", "voice_ctrl_alt_z", 0, {})
    state["signals"]["Activated"]("/session/1", "screenshot_ctrl_shift_s", 0, {})
    # 根治回归：长按快捷键期间 GNOME Portal 以 ~30Hz 连发 Activated，未 Deactivated 前必须全部忽略
    for _ in range(10):
        state["signals"]["Activated"]("/session/1", "screenshot_ctrl_shift_s", 0, {})
    state["signals"]["Deactivated"]("/session/1", "screenshot_ctrl_shift_s", 0, {})
    # 350ms 防抖窗口内再次按下也忽略
    state["signals"]["Activated"]("/session/1", "screenshot_ctrl_shift_s", 0, {})
    state["signals"]["Deactivated"]("/session/1", "screenshot_ctrl_shift_s", 0, {})
    state["signals"]["Activated"]("/session/1", "ocr_ctrl_alt_o", 0, {})
    state["signals"]["Activated"]("/session/1", "ocr_ctrl_alt_o", 0, {})
    state["signals"]["Deactivated"]("/session/1", "ocr_ctrl_alt_o", 0, {})
    state["signals"]["Activated"]("/session/1", "repeat_ctrl_alt_r", 0, {})
    state["signals"]["Deactivated"]("/session/1", "repeat_ctrl_alt_r", 0, {})
    _drain_glib()
    assert events == ["v_start", "v_stop", "shot", "ocr", "repeat"]

    # 模拟 xdg-desktop-portal 退出并重启（owner -> "" -> ":1.88"）：必须调用单源 register_portal_host_app(force=True) 重注册
    regs_before_restart = len(state["host_registers"])
    bus.owner_cb("")
    assert failed_count == 1
    bus.owner_cb(":1.88")
    assert len(state["host_registers"]) == regs_before_restart + 1
    assert register_portal_host_app(bus, force=True, dbus_mod=FakeDbusMod(state)) is True
    assert len(state["host_registers"]) == regs_before_restart + 2
    assert state["create_sessions"] == 2
    state["requests"]["/req/sess/2"](0, {"session_handle": "/session/2"})
    state["requests"]["/req/bind/2"](
        0,
        {
            "shortcuts": [
                ("voice_ctrl_alt_z", {"trigger": "CTRL+ALT+Z"}),
                ("screenshot_ctrl_shift_s", {"trigger": "CTRL+SHIFT+S"}),
                ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
                ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
            ]
        },
    )
    assert bound_count == 2
    assert grabber.mismatched_triggers == {}
    grabber.cleanup()
    assert state["closed_sessions"] == 1


def test_x11_and_evdev_four_hotkeys_dispatch() -> None:
    events: list[str] = []
    actions = CallbackHotkeyActions(
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
    )
    ctx = HotkeyContext(
        actions=actions,
        bindings=DEFAULTS,
        janitor=None,
        guard=InjectGuard(),
        session_type="x11",
    )

    # 1. X11 HotkeyGrabber 四键 grab/ungrab 与分派
    grabbed: list[tuple[int, int]] = []
    ungrabbed: list[tuple[int, int]] = []

    class FakeRoot:
        def grab_key(self, kc, mask, *_rest):
            grabbed.append((kc, mask))

        def ungrab_key(self, kc, mask):
            ungrabbed.append((kc, mask))

    class FakeX11Display:
        def set_error_handler(self, _fn):
            pass

        def screen(self):
            return type("S", (), {"root": FakeRoot()})()

        def keysym_to_keycode(self, sym):
            return sym

        def flush(self):
            pass

        def sync(self):
            pass

    x11 = HotkeyGrabber(
        ctx,
        display_factory=FakeX11Display,
        hotkey_parser=lambda hk: (4, ord(hk.split("+")[-1])),
    )
    x11.grab()
    grabbed_kcs = {kc for kc, _m in grabbed}
    assert grabbed_kcs == {ord("z"), ord("x"), ord("o"), ord("r")}
    x11._handle_event(type("E", (), {"type": _X_KEY_PRESS, "detail": ord("o")})())
    x11._handle_event(type("E", (), {"type": _X_KEY_PRESS, "detail": ord("x")})())
    x11._handle_event(type("E", (), {"type": _X_KEY_PRESS, "detail": ord("r")})())
    x11._handle_event(type("E", (), {"type": _X_KEY_PRESS, "detail": ord("z")})())
    x11._do_release()
    assert events == ["ocr", "shot", "repeat", "v_start", "v_stop"]
    x11.ungrab()
    assert {kc for kc, _m in ungrabbed} == {ord("z"), ord("x"), ord("o"), ord("r")}
    x11.cleanup()

    # 2. EvdevHotkeyGrabber 四键命中分派 + 修饰键不误关浮窗、Esc 正常关闭
    events.clear()
    overlay_state = {"open": True}
    ev_actions = CallbackHotkeyActions(
        on_voice_start=lambda: (events.append("v_start") or True),
        on_voice_stop=lambda: events.append("v_stop"),
        on_screenshot=lambda: events.append("shot"),
        on_ocr=lambda: events.append("ocr"),
        on_repeat=lambda: events.append("repeat"),
        is_overlay_active=lambda: overlay_state["open"],
        on_close_overlay=lambda: (overlay_state.__setitem__("open", False) or events.append("close_ov") or False),
    )
    ev_ctx = HotkeyContext(
        actions=ev_actions,
        bindings=DEFAULTS,
        janitor=None,
        guard=InjectGuard(),
        session_type="wayland",
    )
    fake_dev = type("FakeDev", (), {"fd": 99, "name": "kbd", "path": "/dev/input/event99", "close": lambda self: None})()
    key_map = {"z": 44, "x": 45, "o": 24, "r": 19}
    ev_grabber = EvdevHotkeyGrabber(
        ev_ctx,
        devices=[fake_dev],
        combo_parser=lambda hk: {"trigger": key_map[hk.split("+")[-1]], "modifiers": [{29}, {56}]},
    )
    ev_grabber._handle_key_event(29, 1)  # Ctrl down -> 不误关浮窗
    ev_grabber._handle_key_event(56, 1)  # Alt down -> 不误关浮窗
    ev_grabber._handle_key_event(24, 1)  # O down -> OCR（组合键自身不误关浮窗）
    ev_grabber._handle_key_event(24, 0)
    ev_grabber._handle_key_event(45, 1)  # X down -> Screenshot!
    ev_grabber._handle_key_event(45, 0)
    ev_grabber._handle_key_event(19, 1)  # R down -> Repeat!
    ev_grabber._handle_key_event(19, 0)
    _drain_glib()
    assert overlay_state["open"] is True
    assert events == ["ocr", "shot", "repeat"]
    # 释放修饰键后按 Esc (code=1) 正常关闭活跃浮窗
    ev_grabber._handle_key_event(29, 0)
    ev_grabber._handle_key_event(56, 0)
    ev_grabber._handle_key_event(1, 1)
    _drain_glib()
    assert overlay_state["open"] is False
    assert events[-1] == "close_ov"
    ev_grabber.cleanup()


def test_portal_presubscribes_response_before_dbus_call() -> None:
    """回归：GNOME xdg-desktop-portal 在 CreateSession/BindShortcuts 调用返回前就发出 Response 信号时，
    必须通过预订阅预测路径完整接住，且完成后自动注销一次性 match 不泄漏。"""

    class FakeMatch:
        def __init__(self, path: str, registry: dict) -> None:
            self.path = path
            self.registry = registry
            self.removed = False

        def remove(self) -> None:
            self.removed = True
            self.registry.pop(self.path, None)

    receivers: dict[str, object] = {}
    matches: list[FakeMatch] = []
    events: list[str] = []

    class ImmediatePortalIface:
        def connect_to_signal(self, _name, _cb):
            return FakeMatch("sig", {})

        def CreateSession(self, opts):
            token = str(opts["handle_token"])
            path = f"/org/freedesktop/portal/desktop/request/1_99/{token}"
            assert path in receivers, f"CreateSession 调用前未预订阅 {path}"
            events.append("create_called")
            # 模拟 GNOME 门户在 D-Bus 返回前/同步触发 Response
            receivers[path](0, {"session_handle": "/org/freedesktop/portal/desktop/session/1_99/s1"})
            return path

        def BindShortcuts(self, _sess, shortcuts, _parent, opts):
            token = str(opts["handle_token"])
            path = f"/org/freedesktop/portal/desktop/request/1_99/{token}"
            assert path in receivers, f"BindShortcuts 调用前未预订阅 {path}"
            events.append("bind_called")
            resp_shortcuts = [(sid, {"trigger": props["preferred_trigger"]}) for sid, props in shortcuts]
            cb = receivers[path]
            cb(0, {"shortcuts": resp_shortcuts})
            # 重复触发第二次 Response 必须被一次性守卫忽略，不得重复回调
            cb(0, {"shortcuts": resp_shortcuts})
            return path

        def Register(self, _app_id, _opts):
            pass

        def Close(self):
            pass

    class ImmediateBus:
        def get_unique_name(self) -> str:
            return ":1.99"

        def add_signal_receiver(self, cb, *, signal_name, dbus_interface, bus_name, path):
            assert signal_name == "Response"
            assert dbus_interface == "org.freedesktop.portal.Request"
            assert bus_name == PORTAL_NAME
            receivers[path] = cb
            m = FakeMatch(path, receivers)
            matches.append(m)
            return m

        def get_object(self, bus_name, obj_path):
            return (bus_name, str(obj_path))

        def watch_name_owner(self, _name, cb):
            cb(":1.1")
            return "w"

        def unwatch_name_owner(self, _tok):
            pass

    class ImmediateDbus:
        String = staticmethod(str)
        ObjectPath = staticmethod(str)

        @staticmethod
        def Dictionary(d, signature=None):
            del signature
            return dict(d)

        @staticmethod
        def Array(items, signature=None):
            del signature
            return list(items)

        @staticmethod
        def Interface(_obj, _iface):
            return ImmediatePortalIface()

    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(
            on_voice_start=lambda: True,
            on_voice_stop=lambda: None,
            on_screenshot=lambda: None,
        ),
        bindings=DEFAULTS,
        janitor=None,
        guard=InjectGuard(),
        session_type="wayland",
    )
    bound = []
    grabber = PortalGlobalShortcutsGrabber(
        ctx,
        on_bound=lambda: bound.append(True),
        dbus_mod=ImmediateDbus(),
        bus=ImmediateBus(),
        version_probe=lambda _iface: 2,
    )
    grabber.start()
    assert events == ["create_called", "bind_called"]
    assert bound == [True]
    assert len(matches) == 2 and all(m.removed for m in matches)
    assert grabber._request_matches == []

    # 验证单调序号防同毫秒碰撞 + 调用抛异常时立即回收预订阅 match
    t1 = grabber._new_token("ovi")
    t2 = grabber._new_token("ovi")
    assert t1 != t2 and t1.rsplit("_", 1)[-1] != t2.rsplit("_", 1)[-1]
    before_matches = len(matches)
    try:
        grabber._call_portal_request("err_tok", lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda *_: None)
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert len(matches) == before_matches + 1 and matches[-1].removed is True
    assert grabber._request_matches == []
    grabber.cleanup()


def test_conflict_scanner_known_show_desktop() -> None:
    import subprocess

    def fake_gsettings(cmd, **kw):
        del kw
        out = ""
        if cmd[:2] == ["gsettings", "list-recursively"] and cmd[2] == "org.gnome.desktop.wm.keybindings":
            out = "org.gnome.desktop.wm.keybindings show-desktop ['<Primary><Alt>d']\n"
        elif cmd[:2] == ["gsettings", "get"]:
            out = "@as []\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    conflicts = scan_hotkey_conflicts(
        HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+d", ocr="ctrl+alt+o", repeat="ctrl+alt+r"),
        run=fake_gsettings,
        which=lambda name: "/usr/bin/gsettings" if name == "gsettings" else None,
    ).conflicts
    assert any("show-desktop" in item for item in conflicts), conflicts


def test_portal_discrete_debounce_deactivated_recovery_and_stale_self_heal() -> None:
    """P1-3 回归：Deactivated 正常恢复 + 超 350ms 再次触发 + 长按连发持续抑制 + 丢 Deactivated 超 DISCRETE_STALE_S 自愈。"""
    assert DISCRETE_DEBOUNCE_S == 0.35
    assert DISCRETE_STALE_S == 1.5

    class _Match:
        def remove(self) -> None:
            pass

    signals: dict[str, object] = {}

    class _Iface:
        def connect_to_signal(self, name, cb):
            signals[name] = cb
            return _Match()

        def CreateSession(self, _opts):
            return "/req/s"

        def Register(self, _app_id, _opts):
            pass

        def Close(self):
            pass

    class _Bus:
        def __init__(self) -> None:
            self.owner_cb = None

        def get_object(self, bus_name, obj_path):
            return (bus_name, str(obj_path))

        def watch_name_owner(self, _name, cb):
            self.owner_cb = cb
            cb(":1.1")
            return "w"

        def unwatch_name_owner(self, _tok):
            pass

    class _Dbus:
        String = staticmethod(str)
        ObjectPath = staticmethod(str)

        @staticmethod
        def Dictionary(d, signature=None):
            del signature
            return dict(d)

        @staticmethod
        def Array(items, signature=None):
            del signature
            return list(items)

        @staticmethod
        def Interface(_obj, _iface):
            return _Iface()

    now = [100.0]
    events: list[str] = []
    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(
            on_voice_start=lambda: True,
            on_voice_stop=lambda: None,
            on_screenshot=lambda: events.append("shot"),
            on_ocr=lambda: events.append("ocr"),
            on_repeat=lambda: events.append("repeat"),
        ),
        bindings=DEFAULTS,
        janitor=None,
        guard=InjectGuard(),
        session_type="wayland",
    )
    bus = _Bus()
    grabber = PortalGlobalShortcutsGrabber(
        ctx,
        dbus_mod=_Dbus(),
        bus=bus,
        version_probe=lambda _i: 2,
        clock=lambda: now[0],
    )
    grabber._session_handle = "/session/heal"
    grabber._bound = True

    # 1. 首次按下触发，长按期间每 0.2s 连发（总跨度 2.0s > DISCRETE_STALE_S，但每次间隔 < 1.5s）均持续抑制
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    for _ in range(10):
        now[0] += 0.2
        signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot"]

    # 2. 正常 Deactivated 后清理活跃态与激活时间，超 350ms 再次触发成功
    now[0] += 0.05
    signals["Deactivated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    assert "screenshot" not in grabber._active_shortcuts
    assert "screenshot" not in grabber._last_activated_at

    now[0] += 0.05  # 距上次真实触发(100.0)已 > 0.35s
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot", "shot"]

    # 3. Deactivated 后 < 350ms 内再次按下仍被防抖拦截；超过 350ms 后正常再次触发
    fire2_at = now[0]
    now[0] = fire2_at + 0.05
    signals["Deactivated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    now[0] = fire2_at + 0.20
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    signals["Deactivated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot", "shot"]

    now[0] = fire2_at + 0.40
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot", "shot", "shot"]

    # 4. 故意丢失 Deactivated：1.0s (< 1.5s) 内仍抑制并刷新时间戳，再过 1.55s (>= 1.5s) 自动清除过期活跃态并放行！
    now[0] += 1.0
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot", "shot", "shot"]

    now[0] += 1.55
    signals["Activated"]("/session/heal", "screenshot_ctrl_alt_x", 0, {})
    _drain_glib()
    assert events == ["shot", "shot", "shot", "shot"]

    # 5. Portal 重连与 cleanup 均同步清空三个状态容器
    assert grabber._active_shortcuts and grabber._last_discrete_at and grabber._last_activated_at
    bus.owner_cb("")
    bus.owner_cb(":1.99")
    assert grabber._active_shortcuts == set()
    assert grabber._last_discrete_at == {}
    assert grabber._last_activated_at == {}

    grabber._active_shortcuts.add("ocr")
    grabber._last_discrete_at["ocr"] = now[0]
    grabber._last_activated_at["ocr"] = now[0]
    grabber.cleanup()
    assert grabber._active_shortcuts == set()
    assert grabber._last_discrete_at == {}
    assert grabber._last_activated_at == {}


def main() -> int:
    test_portal_trigger_format_and_fingerprint_id()
    test_accel_normalize()
    test_selection_crop_box()
    test_parse_shortcuts_description()
    test_action_mixin_voice_start_stop_and_default_ocr_noop()
    test_portal_four_hotkeys_rebind_mismatch_and_bus_restart_recovery()
    test_portal_presubscribes_response_before_dbus_call()
    test_x11_and_evdev_four_hotkeys_dispatch()
    test_conflict_scanner_known_show_desktop()
    test_portal_discrete_debounce_deactivated_recovery_and_stale_self_heal()
    print("PASS: portal / 四键动作层纯函数测试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
