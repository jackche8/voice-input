"""快捷键后端测试共用夹具：记录上报的 sink、手动 GLib 定时器、假 evdev 设备、假门户总线、假 X 显示。"""
from __future__ import annotations

from collections import namedtuple

from _linux.portal_dbus import GLOBAL_SHORTCUTS_IFACE, HOST_REGISTRY_IFACE, PORTAL_NAME


class RecordingSink:
    """快捷键 sink 夹具：calls 记区间、放行与 Esc；log 按到达顺序记全部（含读线程进度）。"""

    voice_active = False

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.log: list[tuple] = []

    def _record(self, entry: tuple) -> bool:
        self.calls.append(entry)
        self.log.append(entry)
        return False

    def interval_started(self, action, s_ms):
        return self._record(("start", action, s_ms))

    def interval_ended(self, action, e_ms):
        return self._record(("end", action, e_ms))

    def activated(self, action, t_ms):
        return self._record(("activated", action, t_ms))

    def reader_progress(self, p_ms):
        self.log.append(("progress", p_ms))
        return False

    def escape_pressed(self) -> None:
        self._record(("escape",))

    def source_failed(self, reason):
        return self._record(("source_failed", reason))


class ManualTimers:
    """替换后端模块的 GLib：idle 立即执行，timeout 只登记、由测试 fire_all 手动触发（不起线程、不等时间）。"""

    IO_IN = 1

    def __init__(self) -> None:
        self.timers: dict[int, tuple] = {}
        self._next = 1

    def idle_add(self, fn, *args):
        fn(*args)
        return 0

    def timeout_add(self, _ms, fn, *args):
        sid = self._next
        self._next += 1
        self.timers[sid] = (fn, args)
        return sid

    def source_remove(self, sid):
        return self.timers.pop(sid, None) is not None

    def io_add_watch(self, _fd, _condition, _fn):
        return 0

    def fire_all(self) -> None:
        for sid in list(self.timers):
            fn, args = self.timers.pop(sid)
            fn(*args)


Ev = namedtuple("Ev", "type code value sec usec")
KEYBOARD_KEYS = [1, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 44, 45, 46, 48, 50, 56, 57, 125]


def key_event(code: int, value: int, ms: int) -> Ev:
    """EV_KEY 事件，时间为单调毫秒 ms。"""
    return Ev(1, code, value, ms // 1000, (ms % 1000) * 1000)


class FakeEvdevDevice:
    """假 evdev 设备：能力、当前按键、待读缓冲。"""

    _next_fd = 100

    def __init__(self, path: str, *, name: str = "kbd", keys=KEYBOARD_KEYS, active=()) -> None:
        FakeEvdevDevice._next_fd += 1
        self.fd = FakeEvdevDevice._next_fd
        self.path = path
        self.name = name
        self.keys = list(keys)
        self.active = list(active)
        self.queue: list = []  # 尚未读出的缓冲事件
        self.closed = False

    def capabilities(self):
        return {1: self.keys}

    def active_keys(self):
        return list(self.active)

    def read(self):
        events, self.queue = self.queue, []
        return iter(events)

    def close(self) -> None:
        self.closed = True


class FakePortal:
    """假 xdg-desktop-portal GlobalShortcuts：记录信号订阅与请求，由测试回应绑定、发出放行与改键。"""

    def __init__(self) -> None:
        self.signals: dict = {}
        self.responses: dict = {}
        self.bind_calls: list = []
        self._seq = 0
        portal = self

        class _Match:
            def remove(self) -> None:
                pass

        class _Iface:
            def __init__(self, path: str) -> None:
                self.path = path

            def connect_to_signal(self, name, cb):
                if self.path.startswith("/req/"):
                    portal.responses[self.path] = cb
                else:
                    portal.signals[name] = cb
                return _Match()

            def CreateSession(self, _options):
                portal._seq += 1
                return f"/req/session/{portal._seq}"

            def BindShortcuts(self, _session, shortcuts, _parent, _options):
                portal._seq += 1
                portal.bind_calls.append([sid for sid, _props in shortcuts])
                return f"/req/bind/{portal._seq}"

            def Register(self, _app_id, _options):
                pass

            def Close(self) -> None:
                pass

        class _Bus:
            def get_object(self, _name, path):
                return str(path)

            def watch_name_owner(self, name, cb):
                assert name == PORTAL_NAME
                cb(":1.10")
                return "watch"

            def unwatch_name_owner(self, _token) -> None:
                pass

        class _Dbus:
            String = staticmethod(str)
            ObjectPath = staticmethod(str)

            @staticmethod
            def Dictionary(d, signature=None):
                return dict(d)

            @staticmethod
            def Array(items, signature=None):
                return list(items)

            @staticmethod
            def Interface(obj, iface_name):
                assert iface_name in {
                    GLOBAL_SHORTCUTS_IFACE,
                    HOST_REGISTRY_IFACE,
                    "org.freedesktop.portal.Request",
                    "org.freedesktop.portal.Session",
                }
                return _Iface(obj)

        self.bus = _Bus()
        self.dbus = _Dbus()

    def complete(self, actual: dict[str, str]) -> None:
        """回应建会话与绑定：GNOME 实际绑定 actual（动作 → 触发器文字）。"""
        session_req = next(path for path in self.responses if path.startswith("/req/session/"))
        self.responses.pop(session_req)(0, {"session_handle": "/session/1"})
        bind_req = next(path for path in self.responses if path.startswith("/req/bind/"))
        sids = self.bind_calls[-1]
        self.responses.pop(bind_req)(0, {"shortcuts": [(sid, {"trigger": actual[sid.split("_", 1)[0]]}) for sid in sids]})

    def activate(self, action: str, t_ms: int) -> None:
        sid = next(sid for sid in self.bind_calls[-1] if sid.startswith(f"{action}_"))
        self.signals["Activated"]("/session/1", sid, t_ms, {})


class _FakeX11Root:
    def __init__(self) -> None:
        self.grabbed: list[tuple[int, int]] = []
        self.ungrabbed: list[tuple[int, int]] = []

    def grab_key(self, kc, mask, *_rest):
        self.grabbed.append((kc, mask))

    def ungrab_key(self, kc, mask):
        self.ungrabbed.append((kc, mask))


class FakeX11Display:
    """XGrabKey 用的最小显示：keysym 即键码。"""

    def __init__(self) -> None:
        self.root = _FakeX11Root()

    def set_error_handler(self, _fn):
        pass

    def screen(self):
        return type("S", (), {"root": self.root})()

    def keysym_to_keycode(self, sym):
        return sym

    def flush(self):
        pass

    def sync(self):
        pass

    def fileno(self) -> int:
        return 0


class FakeXInputDisplay:
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
        0xFF1B: 9,  # Escape
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


def simple_hotkey_parser(hotkey_str: str) -> tuple[int, int]:
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
