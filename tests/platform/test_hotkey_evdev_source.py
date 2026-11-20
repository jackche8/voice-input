"""evdev 区间源：按设备记录按下集合并上报按下区间（假设备、假 sink，不打开真实 /dev/input）。"""
from __future__ import annotations

import pytest

import _linux.hotkey_evdev as hotkey_evdev
from _hotkey import HotkeyBindings
from _linux.evdev_keyboards import open_keyboards, readable_keyboard_paths
from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyContext, _FallbackGLib
from _linux.hotkey_evdev import EvdevHotkeyGrabber
from _linux.inject_guard import InjectGuard
from _linux.uinput_device import UINPUT_DEVICE_NAME
from tests.hotkey.fakes import KEYBOARD_KEYS, Ev, FakeEvdevDevice as FakeDevice, RecordingSink, key_event as _key

EV_SYN, EV_KEY, SYN_DROPPED = 0, 1, 3
CTRL, ALT, SUPER, Z, X, C, ESC = 29, 56, 125, 44, 45, 46, 1
BTN_LEFT = 272
VOICE = {"trigger": Z, "modifiers": [{CTRL, 97}, {ALT, 100}]}
SHOT = {"trigger": X, "modifiers": [{CTRL, 97}, {ALT, 100}]}
SUPER_Z = {"trigger": Z, "modifiers": [{SUPER, 126}]}


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(hotkey_evdev, "GLib", _FallbackGLib())  # 投递即执行，顺序即调用顺序

    def _make(*devices, rescan=()):
        ctx = HotkeyContext(
            actions=CallbackHotkeyActions(on_voice_start=lambda: True, on_voice_stop=lambda: None, on_screenshot=lambda: None),
            bindings=HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r"),
            janitor=None,
            guard=InjectGuard(),
            session_type="wayland",
        )
        sink = RecordingSink()
        batches = [list(devices), list(rescan)]
        grabber = EvdevHotkeyGrabber(ctx, sink, open_devices=lambda _exclude: batches.pop(0) if batches else [])
        return grabber, sink

    return _make


def test_interval_starts_when_combo_held_and_ends_at_first_release_in_any_order(make) -> None:
    for first_release in (Z, CTRL, ALT):
        grabber, sink = make(FakeDevice("/dev/input/event5"))
        grabber.set_combos({"voice": VOICE})
        for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
            grabber._handle_event("/dev/input/event5", _key(code, 1, ms))
        grabber._handle_event("/dev/input/event5", _key(first_release, 0, 2000))
        for code in {Z, CTRL, ALT} - {first_release}:
            grabber._handle_event("/dev/input/event5", _key(code, 0, 2100))
        assert sink.calls == [("start", "voice", 1020), ("end", "voice", 2000)], first_release


def test_replacing_combos_follows_the_new_definition(make) -> None:
    grabber, sink = make(FakeDevice("/dev/input/event5"))
    grabber.set_combos({"voice": VOICE})
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        grabber._handle_event("/dev/input/event5", _key(code, 1, ms))
    grabber.set_combos({"voice": SUPER_Z})  # GNOME 实际绑定改成 Super+Z
    assert [c[0] for c in sink.calls] == ["start", "end"]
    grabber._handle_event("/dev/input/event5", _key(SUPER, 1, 3000))
    assert sink.calls[-1] == ("start", "voice", 3000)


def test_new_device_uses_its_current_keys_and_dropped_events_resync(make) -> None:
    held_on_b = FakeDevice("/dev/input/event7", active=[CTRL, ALT, Z, BTN_LEFT])
    grabber, sink = make(FakeDevice("/dev/input/event5"), rescan=[held_on_b])
    grabber.set_combos({"voice": VOICE})
    grabber._rescan()
    assert [c[:2] for c in sink.calls] == [("start", "voice")]

    a = FakeDevice("/dev/input/event5")
    grabber2, sink2 = make(a)
    grabber2.set_combos({"voice": VOICE})
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        grabber2._handle_event("/dev/input/event5", _key(code, 1, ms))
    a.active = []  # 缓冲溢出期间三个键都已松开
    grabber2._handle_event("/dev/input/event5", Ev(EV_SYN, SYN_DROPPED, 0, 4, 500_000))
    assert sink2.calls == [("start", "voice", 1020), ("end", "voice", 4500)]


def test_keyboard_class_devices_are_opened_with_monotonic_clock_and_own_device_excluded() -> None:
    devices = {
        "/dev/input/event3": FakeDevice("/dev/input/event3", name="RustDesk UInput Keyboard", keys=KEYBOARD_KEYS + [BTN_LEFT]),
        "/dev/input/event5": FakeDevice("/dev/input/event5", name="Logitech keyboard"),
        "/dev/input/event9": FakeDevice("/dev/input/event9", name="mouse", keys=[BTN_LEFT, 273]),
        "/dev/input/event14": FakeDevice("/dev/input/event14", name=UINPUT_DEVICE_NAME),
    }

    def open_device(path):
        if path == "/dev/input/event20":
            raise PermissionError(13, "Permission denied")
        return devices[path]

    clocked: list[int] = []
    kwargs = dict(
        list_paths=lambda: [*devices, "/dev/input/event20"],
        open_device=open_device,
        set_clock=clocked.append,
    )
    opened = open_keyboards((), **kwargs)
    assert [d.path for d in opened] == ["/dev/input/event3", "/dev/input/event5"]
    assert clocked == [devices["/dev/input/event3"].fd, devices["/dev/input/event5"].fd]
    assert devices["/dev/input/event9"].closed and devices["/dev/input/event14"].closed
    assert [d.path for d in open_keyboards(("/dev/input/event3",), **kwargs)] == ["/dev/input/event5"]

    for dev in devices.values():
        dev.closed = False
    assert readable_keyboard_paths(**kwargs) == ["/dev/input/event3", "/dev/input/event5"]
    assert all(d.closed for d in devices.values())


def test_escape_is_reported_and_other_keys_are_not(make) -> None:
    grabber, sink = make(FakeDevice("/dev/input/event5"))
    grabber.set_combos({"voice": VOICE})
    for code, ms in ((CTRL, 1000), (C, 1010)):
        grabber._handle_event("/dev/input/event5", _key(code, 1, ms))
    for code, ms in ((C, 1050), (CTRL, 1060)):
        grabber._handle_event("/dev/input/event5", _key(code, 0, ms))
    assert sink.calls == []
    grabber._handle_event("/dev/input/event5", _key(ESC, 1, 2000))
    assert sink.calls == [("escape",)]


def test_activation_is_reported_only_in_fallback_mode_when_trigger_pressed_last(make) -> None:
    grabber, sink = make(FakeDevice("/dev/input/event5"))
    grabber.set_combos({"voice": VOICE, "screenshot": SHOT})
    for code, ms in ((CTRL, 1000), (ALT, 1010), (X, 1020), (X, 1100)):
        grabber._handle_event("/dev/input/event5", _key(code, 1 if ms < 1100 else 0, ms))
    assert sink.calls == [("start", "screenshot", 1020), ("end", "screenshot", 1100)]

    sink.calls.clear()
    grabber.activations_enabled = True
    grabber._handle_event("/dev/input/event5", _key(X, 1, 2000))
    grabber._handle_event("/dev/input/event5", _key(X, 0, 2050))
    assert sink.calls == [
        ("start", "screenshot", 2000),
        ("activated", "screenshot", 2000),
        ("end", "screenshot", 2050),
    ]
    sink.calls.clear()
    grabber._handle_event("/dev/input/event5", _key(ALT, 0, 2900))
    grabber._handle_event("/dev/input/event5", _key(X, 1, 3000))  # trigger 先按住，组合未满足
    grabber._handle_event("/dev/input/event5", _key(ALT, 1, 3020))  # 修饰键最后按下：只有区间，不放行
    assert sink.calls == [("start", "screenshot", 3020)]


def test_reading_current_keys_of_a_vanished_device_counts_as_disconnect(make) -> None:
    def gone():
        raise OSError(19, "No such device")

    a, b = FakeDevice("/dev/input/event5"), FakeDevice("/dev/input/event6")
    grabber, sink = make(a, b)
    grabber.set_combos({"voice": VOICE})
    for code, ms in ((CTRL, 1000), (ALT, 1010), (Z, 1020)):
        grabber._handle_event("/dev/input/event5", _key(code, 1, ms))
    a.active_keys = gone  # 缓冲溢出后重同步时设备已拔出
    grabber._handle_event("/dev/input/event5", Ev(EV_SYN, SYN_DROPPED, 0, 2, 0))
    grabber._handle_event("/dev/input/event5", _key(CTRL, 1, 2100))  # 同批里它剩下的事件不再计入
    assert sink.calls == [("start", "voice", 1020), ("end", "voice", 2000)]
    assert a.closed and grabber.any_modifier_down() is False
    for code, ms in ((CTRL, 3000), (ALT, 3010), (Z, 3020)):  # 读线程照常：另一设备继续上报
        grabber._handle_event("/dev/input/event6", _key(code, 1, ms))
    assert sink.calls[-1] == ("start", "voice", 3020)

    late = FakeDevice("/dev/input/event7")
    late.active_keys = gone  # 重扫打开后、读当前按键前拔出：跳过，不让读线程退出
    grabber2, _sink2 = make(FakeDevice("/dev/input/event5"), rescan=[late])
    grabber2._rescan()
    assert late.closed and "/dev/input/event7" not in grabber2._devices


def test_progress_is_withheld_while_a_device_read_comes_back_full(make, monkeypatch) -> None:
    dev = FakeDevice("/dev/input/event5")
    grabber, sink = make(dev)
    grabber.set_combos({"voice": VOICE})
    # 读线程滞后：缓冲里积压 70 个事件，一次只能读 64 个
    dev.queue = [_key(C, value, 1000 + i) for i, value in zip(range(70), [1, 0] * 35)]
    clock = iter([2000, 2500])  # 两轮各取一次本轮开始时间
    monkeypatch.setattr(hotkey_evdev, "_now_ms", lambda: next(clock))
    monkeypatch.setattr(hotkey_evdev, "select", _FakeSelect([dev.fd]))
    grabber._poll_once()
    assert [entry for entry in sink.log if entry[0] == "progress"] == []  # 剩余事件未读，进度不能越过它们
    grabber._poll_once()
    assert [entry for entry in sink.log if entry[0] == "progress"] == [("progress", 2500)]


class _FakeSelect:
    """替换读线程模块里的 select：返回预设的就绪 fd，并可在 select 期间模拟新事件入队。"""

    def __init__(self, ready_fds, during=None) -> None:
        self.ready_fds = list(ready_fds)
        self.during = during

    def select(self, _rlist, _wlist, _xlist, _timeout):
        if self.during is not None:
            self.during()
        return list(self.ready_fds), [], []


def test_round_merges_ready_devices_by_time_and_progress_never_passes_unread_events(make, monkeypatch) -> None:
    # 同一把键盘的两个设备节点：修饰键在 event5，Z 在 event6；读线程滞后，一轮里两边都已积压
    a, b = FakeDevice("/dev/input/event5"), FakeDevice("/dev/input/event6")
    grabber, sink = make(a, b)
    grabber.set_combos({"voice": VOICE})
    a.queue = [_key(CTRL, 1, 1000), _key(ALT, 1, 1010), _key(CTRL, 0, 1200), _key(ALT, 0, 1210)]
    b.queue = [_key(Z, 1, 1020), _key(Z, 0, 1100)]
    clock = iter([1500, 1700, 1700, 1700])  # 本轮开始 1500；之后的取时都更晚
    monkeypatch.setattr(hotkey_evdev, "_now_ms", lambda: next(clock))
    monkeypatch.setattr(hotkey_evdev, "select", _FakeSelect([a.fd, b.fd], during=lambda: b.queue.append(_key(Z, 1, 1600))))
    grabber._poll_once()
    # 按时间合并：Ctrl 1000、Alt 1010、Z 1020 → 一个区间；select 期间入队的 1600 本轮也读到
    assert sink.log[:2] == [("start", "voice", 1020), ("end", "voice", 1100)]
    # 进度只到本轮开始（1500），不越过本轮之后才可能入队的事件
    assert sink.log[-1] == ("progress", 1500)


def test_rescan_skips_devices_that_fail_mid_probe_and_reader_keeps_reporting(monkeypatch) -> None:
    monkeypatch.setattr(hotkey_evdev, "GLib", _FallbackGLib())
    first = FakeDevice("/dev/input/event5")
    unplugged = FakeDevice("/dev/input/event8")
    refuses_ioctl = FakeDevice("/dev/input/event9")
    late = FakeDevice("/dev/input/event10")

    def capabilities_gone():
        raise OSError(19, "No such device")

    unplugged.capabilities = capabilities_gone
    present = {d.path: d for d in (first,)}

    def set_clock(fd):
        if fd == refuses_ioctl.fd:
            raise OSError(22, "Invalid argument")

    def open_devices(exclude):
        return open_keyboards(exclude, list_paths=lambda: list(present), open_device=present.__getitem__, set_clock=set_clock)

    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(on_voice_start=lambda: True, on_voice_stop=lambda: None, on_screenshot=lambda: None),
        bindings=HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r"),
        janitor=None,
        guard=InjectGuard(),
        session_type="wayland",
    )
    sink = RecordingSink()
    grabber = EvdevHotkeyGrabber(ctx, sink, open_devices=open_devices)
    grabber.set_combos({"voice": VOICE})
    present.update({d.path: d for d in (unplugged, refuses_ioctl, late)})
    grabber._rescan()  # 探测途中断开、拒绝 ioctl 的两个设备被跳过，不抛出
    assert unplugged.closed and refuses_ioctl.closed and not late.closed
    late.queue = [_key(CTRL, 1, 1000), _key(ALT, 1, 1010), _key(Z, 1, 1020)]
    monkeypatch.setattr(hotkey_evdev, "select", _FakeSelect([late.fd]))
    grabber._poll_once()
    assert sink.calls == [("start", "voice", 1020)]


def test_unexpected_reader_failure_is_reported_and_ends_the_thread(make, monkeypatch) -> None:
    dev = FakeDevice("/dev/input/event5")
    grabber, sink = make(dev)

    def broken_read():
        raise RuntimeError("boom")

    dev.read = broken_read
    monkeypatch.setattr(hotkey_evdev, "select", _FakeSelect([dev.fd]))
    grabber._running = True
    grabber._monitor()  # 在测试线程里直接跑读线程主体：应上报并返回，而不是抛出
    assert grabber._running is False
    assert sink.calls == [("source_failed", "evdev 读线程意外退出：RuntimeError: boom")]
