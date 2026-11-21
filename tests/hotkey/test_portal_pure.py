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
from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyContext  # noqa: E402
from _linux.hotkey_portal import (  # noqa: E402
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
from tests.hotkey.fakes import FakeX11Display, ManualTimers, RecordingSink  # noqa: E402

DEFAULTS = HotkeyBindings(
    voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r", annotate="ctrl+alt+a"
)


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
    assert to_portal_trigger("ctrl+alt+a") == "CTRL+ALT+A"
    assert to_portal_trigger("ctrl+grave") == "CTRL+grave"
    assert portal_shortcut_id("screenshot", "ctrl+alt+x") == "screenshot_ctrl_alt_x"
    assert portal_shortcut_id("screenshot", "Ctrl+Shift+S") == "screenshot_ctrl_shift_s"
    assert portal_shortcut_id("ocr", "ctrl+alt+o") == "ocr_ctrl_alt_o"
    assert portal_shortcut_id("annotate", "ctrl+alt+a") == "annotate_ctrl_alt_a"
    assert action_from_shortcut_id("screenshot_ctrl_shift_s") == "screenshot"
    assert action_from_shortcut_id("ocr_ctrl_alt_o") == "ocr"
    assert action_from_shortcut_id("annotate_ctrl_alt_a") == "annotate"
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
        ("annotate", {"trigger": "CTRL+ALT+A"}),
    ]
    parsed = grabber._parse_shortcuts(raw)
    assert parsed["voice"] == "按 <Control><Alt>z"
    assert parsed["screenshot"] == "CTRL+ALT+X"
    assert parsed["ocr"] == "CTRL+ALT+O"
    assert parsed["repeat"] == "CTRL+ALT+R"
    assert parsed["annotate"] == "CTRL+ALT+A"


def test_portal_four_hotkeys_rebind_mismatch_and_bus_restart_recovery(tmp_path, monkeypatch) -> None:
    """验证 Portal 四键指纹 ID、改键不匹配自动调 ConfigureShortcuts、以及 Portal 总线重启自动重注册宿主。"""
    entry = tmp_path / "applications" / "ollama-voice-input-daemon.desktop"  # 门户条目按 XDG 数据目录查找
    entry.parent.mkdir()
    entry.write_text("[Desktop Entry]\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

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
    actions = CallbackHotkeyActions(
        on_voice_start=lambda: True,
        on_voice_stop=lambda: None,
        on_screenshot=lambda: None,
    )
    custom_bindings = HotkeyBindings(
        voice="ctrl+alt+z", screenshot="ctrl+shift+s", ocr="ctrl+alt+o", repeat="ctrl+alt+r", annotate="ctrl+alt+a"
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

    sink = RecordingSink()
    reported_triggers: list[dict[str, str]] = []
    grabber = PortalGlobalShortcutsGrabber(
        ctx,
        sink,
        on_bound=on_bound,
        on_failed=on_failed,
        on_triggers=reported_triggers.append,
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
        "annotate_ctrl_alt_a",
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
                ("annotate_ctrl_alt_a", {"trigger": "CTRL+ALT+A"}),
            ]
        },
    )
    assert bound_count == 1
    assert "screenshot" in grabber.mismatched_triggers
    assert state["configure_calls"] == ["/session/1"]
    # GNOME 实际绑定（不是配置）交给 manager 下发给 evdev
    assert reported_triggers[-1] == {
        "voice": "CTRL+ALT+Z",
        "screenshot": "CTRL+ALT+X",
        "ocr": "CTRL+ALT+O",
        "repeat": "CTRL+ALT+R",
        "annotate": "CTRL+ALT+A",
    }

    # 根治回归：ShortcutsChanged 信号到来时同步重算 mismatched_triggers（修复后清空，新冲突即时反映，且不死循环弹 ConfigureShortcuts）
    state["signals"]["ShortcutsChanged"](
        "/session/1",
        [
            ("voice_ctrl_alt_z", {"trigger": "Press Ctrl+Alt+Z"}),
            ("screenshot_ctrl_shift_s", {"trigger": "SHIFT+CTRL+S"}),
            ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
            ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
            ("annotate_ctrl_alt_a", {"trigger": "CTRL+ALT+A"}),
        ],
    )
    assert grabber.mismatched_triggers == {}
    assert state["configure_calls"] == ["/session/1"]
    assert reported_triggers[-1]["screenshot"] == "SHIFT+CTRL+S"

    state["signals"]["ShortcutsChanged"](
        "/session/1",
        [
            ("voice_ctrl_alt_z", {"trigger": "CTRL+ALT+Z"}),
            ("screenshot_ctrl_shift_s", {"trigger": "CTRL+ALT+X"}),
            ("ocr_ctrl_alt_o", {"trigger": "CTRL+ALT+O"}),
            ("repeat_ctrl_alt_r", {"trigger": "CTRL+ALT+R"}),
            ("annotate_ctrl_alt_a", {"trigger": "CTRL+ALT+A"}),
        ],
    )
    assert "screenshot" in grabber.mismatched_triggers
    assert state["configure_calls"] == ["/session/1"]

    # 门户只上报放行（带合成器时间戳），不订阅 Deactivated；别的会话与未知 ID 不上报
    assert "Deactivated" not in state["signals"]
    state["signals"]["Activated"]("/session/1", "voice_ctrl_alt_z", 1000, {})
    state["signals"]["Activated"]("/session/1", "screenshot_ctrl_shift_s", 2000, {})
    state["signals"]["Activated"]("/session/1", "screenshot_ctrl_shift_s", 2500, {})
    state["signals"]["Activated"]("/session/1", "ocr_ctrl_alt_o", 3000, {})
    state["signals"]["Activated"]("/session/1", "repeat_ctrl_alt_r", 4000, {})
    state["signals"]["Activated"]("/session/1", "annotate_ctrl_alt_a", 4500, {})
    state["signals"]["Activated"]("/session/other", "voice_ctrl_alt_z", 5000, {})
    state["signals"]["Activated"]("/session/1", "unknown_shortcut", 6000, {})
    _drain_glib()
    assert sink.calls == [
        ("activated", "voice", 1000),
        ("activated", "screenshot", 2000),
        ("activated", "screenshot", 2500),
        ("activated", "ocr", 3000),
        ("activated", "repeat", 4000),
        ("activated", "annotate", 4500),
    ]

    # 模拟 xdg-desktop-portal 退出并重启（owner -> "" -> ":1.88"）：必须调用单源 register_portal_host_app(force=True) 重注册
    regs_before_restart = len(state["host_registers"])
    bus.owner_cb("")
    assert failed_count == 1
    bus.owner_cb(":1.88")
    assert len(state["host_registers"]) == regs_before_restart + 1
    assert register_portal_host_app(bus, force=True, dbus_mod=FakeDbusMod(state)) is True
    assert len(state["host_registers"]) == regs_before_restart + 2
    assert state["host_registers"][-1] == ("ollama-voice-input-daemon", {"desktop_entry": str(entry)})
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
                ("annotate_ctrl_alt_a", {"trigger": "CTRL+ALT+A"}),
            ]
        },
    )
    assert bound_count == 2
    assert grabber.mismatched_triggers == {}
    grabber.cleanup()
    assert state["closed_sessions"] == 1


def test_x11_reports_intervals_and_filters_autorepeat_release_pairs() -> None:
    import _linux.hotkey_x11 as hotkey_x11

    sink = RecordingSink()
    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(on_voice_start=lambda: True, on_voice_stop=lambda: None, on_screenshot=lambda: None),
        bindings=DEFAULTS,
        janitor=None,
        guard=InjectGuard(),
        session_type="x11",
    )
    def key(kind, ch, ms):
        return type("E", (), {"type": kind, "detail": ord(ch), "time": ms})()

    timers = ManualTimers()
    saved_glib = hotkey_x11.GLib
    hotkey_x11.GLib = timers
    try:
        x11 = HotkeyGrabber(
            ctx,
            sink,
            display_factory=FakeX11Display,
            hotkey_parser=lambda hk: (4, ord(hk.split("+")[-1])),
        )
        x11.grab()
        assert {kc for kc, _m in x11.dpy.root.grabbed} == {ord("z"), ord("x"), ord("o"), ord("r"), ord("a")}
        x11._handle_event(key(_X_KEY_PRESS, "o", 1000))
        x11._handle_event(key(_X_KEY_RELEASE, "o", 1100))  # X 自动重复：松开后同一时刻又按下
        x11._handle_event(key(_X_KEY_PRESS, "o", 1100))
        x11._handle_event(key(_X_KEY_RELEASE, "o", 1500))
        timers.fire_all()
        for ch, action, ms in (("x", "screenshot", 2000), ("r", "repeat", 3000), ("a", "annotate", 3500), ("z", "voice", 4000)):
            x11._handle_event(key(_X_KEY_PRESS, ch, ms))
            x11._handle_event(key(_X_KEY_RELEASE, ch, ms + 50))
            timers.fire_all()
        assert sink.calls == [
            ("start", "ocr", 1000),
            ("activated", "ocr", 1000),
            ("end", "ocr", 1500),
            ("start", "screenshot", 2000),
            ("activated", "screenshot", 2000),
            ("end", "screenshot", 2050),
            ("start", "repeat", 3000),
            ("activated", "repeat", 3000),
            ("end", "repeat", 3050),
            ("start", "annotate", 3500),
            ("activated", "annotate", 3500),
            ("end", "annotate", 3550),
            ("start", "voice", 4000),
            ("activated", "voice", 4000),
            ("end", "voice", 4050),
        ]
        x11.ungrab()
        assert {kc for kc, _m in x11.dpy.root.ungrabbed} == {ord("z"), ord("x"), ord("o"), ord("r"), ord("a")}
        x11.cleanup()
    finally:
        hotkey_x11.GLib = saved_glib


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
        RecordingSink(),
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
        HotkeyBindings(
            voice="ctrl+alt+z", screenshot="ctrl+alt+d", ocr="ctrl+alt+o", repeat="ctrl+alt+r", annotate="ctrl+alt+a"
        ),
        run=fake_gsettings,
        which=lambda name: "/usr/bin/gsettings" if name == "gsettings" else None,
    ).conflicts
    assert any("show-desktop" in item for item in conflicts), conflicts


def main() -> int:
    test_portal_trigger_format_and_fingerprint_id()
    test_accel_normalize()
    test_selection_crop_box()
    test_parse_shortcuts_description()
    test_portal_four_hotkeys_rebind_mismatch_and_bus_restart_recovery()
    test_portal_presubscribes_response_before_dbus_call()
    test_x11_reports_intervals_and_filters_autorepeat_release_pairs()
    test_conflict_scanner_known_show_desktop()
    print("PASS: portal / 四键动作层纯函数测试")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
