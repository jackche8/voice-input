"""T4 回放（六.1 自动化）：真实门户后端（假总线）+ 真实 evdev（假设备）+ 真实 manager / 分发器 / 按住状态；
X11 用真实 XGrabKey（假显示）。按事件到达顺序直接驱动各后端，不起线程、不打开真实设备、不触发真实热键。"""
from __future__ import annotations

import random

import pytest

import _linux.hotkey_dispatch as hotkey_dispatch
import _linux.hotkey_evdev as hotkey_evdev
import _linux.hotkey_portal as hotkey_portal
import _linux.hotkey_x11 as hotkey_x11
import _linux.hotkey_xinput as hotkey_xinput
from _hotkey import HotkeyBindings
from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyContext
from _linux.hotkey_evdev import EvdevHotkeyGrabber
from _linux.hotkey_manager import HotkeyManager
from _linux.hotkey_portal import PortalGlobalShortcutsGrabber
from _linux.hotkey_x11 import _X_KEY_PRESS, _X_KEY_RELEASE, HotkeyGrabber
from _linux.hotkey_xinput import XInputHotkeyGrabber
from _linux.inject_guard import InjectGuard
from tests.hotkey.fakes import (
    FakeEvdevDevice,
    FakePortal,
    FakeX11Display,
    FakeXInputDisplay,
    ManualTimers,
    key_event,
    simple_hotkey_parser,
)

CONFIG = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r")
GNOME = {"voice": "CTRL+ALT+Z", "screenshot": "CTRL+ALT+X", "ocr": "CTRL+ALT+O", "repeat": "CTRL+ALT+R"}
CTRL, ALT, SUPER, Z, X, BTN_LEFT = 29, 56, 125, 44, 45, 272
KB, MOUSE = "/dev/input/event5", "/dev/input/event16"


class Recorder:
    """动作与修饰键清理的记录器（扮演桌面动作与 ModifierJanitor）。"""

    def __init__(self, *, start_ok: bool = True) -> None:
        self.events: list[str] = []
        self.released: list[str] = []
        self.start_ok = start_ok
        self.evdev_any_modifier_down = None

    def actions(self) -> CallbackHotkeyActions:
        return CallbackHotkeyActions(
            on_voice_start=lambda: (self.events.append("v_start") or self.start_ok),
            on_voice_stop=lambda: self.events.append("v_stop"),
            on_screenshot=lambda: self.events.append("shot"),
            on_ocr=lambda: self.events.append("ocr"),
            on_repeat=lambda: self.events.append("repeat"),
        )

    def release_hotkey(self, hotkey: str) -> None:
        self.released.append(hotkey)


@pytest.fixture
def timers(monkeypatch) -> ManualTimers:
    manual = ManualTimers()
    for module in (hotkey_dispatch, hotkey_evdev, hotkey_portal, hotkey_x11, hotkey_xinput):
        monkeypatch.setattr(module, "GLib", manual)
    return manual


class WaylandRig:
    def __init__(self, *, actual=GNOME, devices=(KB,), evdev_ok=True, xinput=False, start_ok=True) -> None:
        self.rec = Recorder(start_ok=start_ok)
        self.errors: list[str | None] = []
        self.devices = {path: FakeEvdevDevice(path) for path in devices}
        self.portal = FakePortal()
        self.portal_created = False
        ctx = HotkeyContext(
            actions=self.rec.actions(), bindings=CONFIG, janitor=self.rec, guard=InjectGuard(), session_type="wayland"
        )

        def make_evdev(c, sink):
            if not evdev_ok:
                raise RuntimeError("未找到可读的键盘设备")
            grabber = EvdevHotkeyGrabber(c, sink, open_devices=lambda ex: [d for p, d in self.devices.items() if p not in ex])
            grabber.start = lambda: None  # 读线程由测试按到达顺序直接驱动
            return grabber

        def make_portal(c, sink, **callbacks):
            self.portal_created = True
            return PortalGlobalShortcutsGrabber(
                c, sink, dbus_mod=self.portal.dbus, bus=self.portal.bus, version_probe=lambda _i: 2, **callbacks
            )

        def make_xinput(c, sink):
            if not xinput:
                raise RuntimeError("无 X 显示")
            return XInputHotkeyGrabber(c, sink, display_factory=FakeXInputDisplay, hotkey_parser=simple_hotkey_parser)

        self.manager = HotkeyManager(
            ctx,
            on_error=self.errors.append,
            evdev_factory=make_evdev,
            portal_factory=make_portal,
            portal_available=lambda: True,
            xinput_factory=make_xinput,
        )
        self.manager.start()
        if evdev_ok:
            self.portal.complete(actual)

    def key(self, code: int, value: int, ms: int, path: str = KB) -> None:
        self.manager._evdev._handle_event(path, key_event(code, value, ms))

    def activate(self, action: str, ms: int) -> None:
        self.portal.activate(action, ms)

    def failures(self) -> list[str]:
        return [e for e in self.errors if e]

    def kill_reader(self, monkeypatch) -> None:
        """读线程运行中遇到意外异常：在测试线程里直接跑读线程主体到它退出。"""

        def broken_read():
            raise RuntimeError("boom")

        self.devices[KB].read = broken_read
        monkeypatch.setattr(hotkey_evdev, "select", type("S", (), {"select": staticmethod(lambda r, w, x, t: (list(r), [], []))}))
        evdev = self.manager._evdev
        evdev._running = True
        evdev._monitor()


def test_any_release_order_stops_once_and_never_double_starts(timers) -> None:
    rig = WaylandRig()
    rnd = random.Random(20261119)
    t = 10_000
    for _ in range(100):
        rig.key(CTRL, 1, t)
        rig.key(ALT, 1, t + 10)
        s = t + 20
        early = rnd.random() < 0.5  # 放行比 evdev 早到 / 晚到（0–10ms 内的先后，表现为处理顺序）
        if early:
            rig.activate("voice", s)
        rig.key(Z, 1, s)
        if not early:
            rig.activate("voice", s)
        for k in range(rnd.randint(0, 5)):  # 按住期间约 33Hz 的连发
            rig.activate("voice", s + 500 + 30 * k)
        t = s + 700
        order = [CTRL, ALT, Z]
        rnd.shuffle(order)
        for code in order:
            rig.key(code, 0, t)
            t += rnd.randint(0, 40)
        if rnd.random() < 0.3:
            rig.activate("voice", t + 2)  # 计时器时间晚于松手的迟到连发
        t += 300
    assert rig.rec.events == ["v_start", "v_stop"] * 100
    assert rig.failures() == []


def test_short_taps_all_fire_and_holding_does_not_repeat(timers) -> None:
    rig = WaylandRig()
    rnd = random.Random(7)
    t = 10_000
    for _ in range(20):
        rig.key(CTRL, 1, t)
        rig.key(ALT, 1, t + 5)
        s = t + 10
        late = rnd.random() < 0.5
        if not late:
            rig.activate("screenshot", s)
        rig.key(X, 1, s)
        rig.key(X, 0, s + rnd.randint(20, 90))  # 短按 <100ms
        if late:
            rig.activate("screenshot", s)  # 放行在松开之后才到
        rig.key(ALT, 0, s + 120)
        rig.key(CTRL, 0, s + 130)
        t = s + 400
    assert rig.rec.events == ["shot"] * 20

    rig.key(CTRL, 1, t)
    rig.key(ALT, 1, t + 5)
    rig.key(X, 1, t + 10)
    rig.activate("screenshot", t + 10)
    for k in range(10):
        rig.activate("screenshot", t + 510 + 30 * k)
    rig.key(X, 0, t + 900)
    assert rig.rec.events == ["shot"] * 21


def test_actual_gnome_binding_defines_the_combo(timers) -> None:
    rig = WaylandRig(actual={**GNOME, "voice": "SUPER+Z"})
    rig.key(SUPER, 1, 1000)
    rig.key(Z, 1, 1010)
    rig.activate("voice", 1010)
    rig.key(SUPER, 0, 1500)
    rig.key(Z, 0, 1510)
    assert rig.rec.events == ["v_start", "v_stop"]
    for code in (CTRL, ALT, Z):  # 配置里的 Ctrl+Alt+Z 已不是 GNOME 的绑定：evdev 不判区间
        rig.key(code, 1, 2000)
    rig.activate("voice", 2000)
    assert rig.rec.events == ["v_start", "v_stop"]
    rig.manager.reset_voice()
    assert rig.rec.released == ["super+z"]  # 看门狗复位按实际生效的组合补发（8.16 R6）


def test_other_device_disconnect_does_not_end_but_holding_device_does_and_mouse_buttons_do_nothing(timers) -> None:
    rig = WaylandRig(devices=(KB, MOUSE))
    rig.key(BTN_LEFT, 1, 900, path=MOUSE)  # 鼠标按键不进按键逻辑
    rig.key(BTN_LEFT, 0, 950, path=MOUSE)
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        rig.key(code, 1, ms)
    rig.activate("voice", 1020)
    rig.key(BTN_LEFT, 1, 1100, path=MOUSE)
    rig.manager._evdev._drop_device(MOUSE, 1150)  # 无线鼠标掉线
    assert rig.rec.events == ["v_start"]
    rig.manager._evdev._drop_device(KB, 1200)  # 按住组合的键盘断开 = 物理上不再按住
    assert rig.rec.events == ["v_start", "v_stop"]


def test_xinput_does_not_enter_the_state_machine_on_wayland(timers) -> None:
    rig = WaylandRig(xinput=True)
    xinput = rig.manager._xinput
    assert xinput.actions_enabled is False
    for keycode in (37, 64, 52):  # Ctrl、Alt、Z（XWayland 上的 XTEST 注入）
        xinput._handle_press(keycode, 0, 1000)
    xinput._handle_press(9, 0, 1100)  # Esc
    assert rig.rec.events == []


def test_unreadable_evdev_fails_only_the_hotkey_component(timers) -> None:
    rig = WaylandRig(evdev_ok=False)
    assert rig.failures() and "Wayland 下快捷键需要读取键盘设备" in rig.failures()[0]
    assert rig.portal_created is False and rig.manager.backend_name == "none"


def test_unparseable_actual_binding_is_reported_and_other_combos_still_work(timers) -> None:
    rig = WaylandRig(actual={**GNOME, "screenshot": "HYPER+X"})
    assert len(rig.failures()) == 1 and "screenshot='hyper+x'" in rig.failures()[0]
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        rig.key(code, 1, ms)
    rig.activate("voice", 1020)
    rig.key(Z, 0, 1500)
    assert rig.rec.events == ["v_start", "v_stop"]


def test_reader_failure_reaches_on_error(timers, monkeypatch) -> None:
    rig = WaylandRig()
    rig.kill_reader(monkeypatch)
    assert rig.failures() == ["evdev 读线程意外退出：RuntimeError: boom"]


def test_source_failure_while_recording_stops_the_recording(timers, monkeypatch) -> None:
    rig = WaylandRig()
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        rig.key(code, 1, ms)
    rig.activate("voice", 1020)
    rig.kill_reader(monkeypatch)  # 之后再也收不到松开
    assert rig.rec.events == ["v_start", "v_stop"]
    assert rig.manager.dispatcher.voice_active is False


def test_failures_are_kept_per_kind_and_source_exit_survives_binding_recovery(timers, monkeypatch) -> None:
    rig = WaylandRig(actual={**GNOME, "screenshot": "HYPER+X"})
    rig.kill_reader(monkeypatch)
    binding, source = "screenshot='hyper+x'", "evdev 读线程意外退出：RuntimeError: boom"
    assert binding in rig.errors[-1] and source in rig.errors[-1]  # 两种失败同时可见
    rig.portal.change(GNOME)  # 用户在 GNOME 里把截图键改回可解析的绑定：只撤销绑定这一种
    assert rig.errors[-1] == source
    rig.portal.change(GNOME)  # 合并原因没变：不重复上报
    assert rig.errors.count(source) == 1


def test_x11_trigger_release_ends_and_modifiers_are_released_only_when_it_stopped_recording(timers) -> None:
    for start_ok in (True, False):
        rec = Recorder(start_ok=start_ok)
        ctx = HotkeyContext(actions=rec.actions(), bindings=CONFIG, janitor=rec, guard=InjectGuard(), session_type="x11")
        manager = HotkeyManager(
            ctx,
            on_error=lambda _r: None,
            x11_factory=lambda c, sink: HotkeyGrabber(
                c, sink, display_factory=FakeX11Display, hotkey_parser=lambda hk: (4, ord(hk.split("+")[-1]))
            ),
        )
        manager.start()
        x11 = manager._x11

        def key(kind, ch, ms):
            x11._handle_event(type("E", (), {"type": kind, "detail": ord(ch), "time": ms})())

        key(_X_KEY_PRESS, "z", 1000)
        key(_X_KEY_RELEASE, "z", 1100)  # X 自动重复：松开后同一时刻又按下
        key(_X_KEY_PRESS, "z", 1100)
        key(_X_KEY_RELEASE, "z", 1500)
        timers.fire_all()
        for ms in (2000, 3000):  # 离散键每次按下一次，自动重复不连发
            key(_X_KEY_PRESS, "x", ms)
            key(_X_KEY_RELEASE, "x", ms + 30)
            key(_X_KEY_PRESS, "x", ms + 30)
            key(_X_KEY_RELEASE, "x", ms + 80)
            timers.fire_all()
        if start_ok:
            assert rec.events == ["v_start", "v_stop", "shot", "shot"]
            assert rec.released == ["ctrl+alt+z"]
        else:
            assert rec.events == ["v_start", "shot", "shot"]  # 开录失败：这次松开没停录音，不补发
            assert rec.released == []
        manager.cleanup()
