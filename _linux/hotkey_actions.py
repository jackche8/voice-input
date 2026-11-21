"""快捷键后端共用的接口：GLib 调度适配、动作与上下文、后端上报的 sink 协议、自判开始后端的结束补发。

后端只把按键事件翻译成 sink 上报（按下区间、放行、Esc、事件源失败），不持有按住状态；状态与执行动作在
`_linux/hotkey_dispatch.py`。当运行在无 `gi`（PyGObject）的隔离测试或纯 Qt 环境时，自动回退线程安全的定时器/即时调度。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class _FallbackGLib:
    """无 PyGObject 环境下的轻量调度兼容层（供隔离单测与非 GTK 宿主安全运行）。"""

    IO_IN = 1

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._timers: dict[int, threading.Timer] = {}

    def idle_add(self, fn: Callable, *args) -> int:
        fn(*args)
        return 0

    def timeout_add(self, interval_ms: int, fn: Callable, *args) -> int:
        with self._lock:
            sid = self._next_id
            self._next_id += 1

        def _fire() -> None:
            with self._lock:
                self._timers.pop(sid, None)
            fn(*args)

        timer = threading.Timer(max(0.0, interval_ms / 1000.0), _fire)
        timer.daemon = True
        with self._lock:
            self._timers[sid] = timer
        timer.start()
        return sid

    def source_remove(self, sid: int) -> bool:
        with self._lock:
            timer = self._timers.pop(sid, None)
        if timer is not None:
            timer.cancel()
            return True
        return False

    def io_add_watch(self, fd: int, condition: int, fn: Callable) -> int:
        del fd, condition, fn
        return 0


try:
    from gi.repository import GLib  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - used in isolated venv-test without PyGObject
    GLib = _FallbackGLib()  # type: ignore[assignment]

from _hotkey import HotkeyBindings  # noqa: E402
from _linux.hotkey_state import Action  # noqa: E402
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.modifiers import ModifierJanitor  # noqa: E402


class HotkeyActions(Protocol):
    def voice_start(self) -> bool: ...

    def voice_stop(self) -> None: ...

    def screenshot(self) -> None: ...

    def ocr(self) -> None: ...

    def repeat(self) -> None: ...

    def annotate(self) -> None: ...

    def overlay_active(self) -> bool: ...

    def close_overlay(self) -> bool: ...


@dataclass
class CallbackHotkeyActions:
    """基于函数回调的 `HotkeyActions` 实现，完整支持五键。"""

    on_voice_start: Callable[[], bool]
    on_voice_stop: Callable[[], None]
    on_screenshot: Callable[[], None]
    on_repeat: Callable[[], None] = lambda: None
    on_ocr: Callable[[], None] = lambda: None
    on_annotate: Callable[[], None] = lambda: None
    is_overlay_active: Callable[[], bool] = lambda: False
    on_close_overlay: Callable[[], bool] = lambda: False

    def voice_start(self) -> bool:
        return self.on_voice_start()

    def voice_stop(self) -> None:
        self.on_voice_stop()

    def screenshot(self) -> None:
        self.on_screenshot()

    def ocr(self) -> None:
        self.on_ocr()

    def repeat(self) -> None:
        self.on_repeat()

    def annotate(self) -> None:
        self.on_annotate()

    def overlay_active(self) -> bool:
        return self.is_overlay_active()

    def close_overlay(self) -> bool:
        return self.on_close_overlay()


@dataclass
class HotkeyContext:
    actions: HotkeyActions
    bindings: HotkeyBindings
    janitor: ModifierJanitor
    guard: InjectGuard
    session_type: str


class HotkeySink(Protocol):
    """后端上报按下区间与放行的唯一去处（主线程执行动作，单一按住状态）。"""

    @property
    def voice_active(self) -> bool: ...

    def interval_started(self, action: Action, s_ms: int) -> bool: ...

    def interval_ended(self, action: Action, e_ms: int) -> bool: ...

    def activated(self, action: Action, t_ms: int) -> bool: ...

    def reader_progress(self, p_ms: int) -> bool: ...

    def escape_pressed(self) -> None: ...

    def source_failed(self, reason: str) -> bool: ...


def end_interval_and_release(sink: HotkeySink, ctx: HotkeyContext, action: Action, ts_ms: int) -> bool:
    """自行判定开始的后端（X11、回退模式）在主线程报结束；这次结束停了录音时补发语音组合的修饰键抬起。

    补发是这些后端的现状语义（门户模式不补发），归后端、不归分发器；可作 GLib.idle_add 回调。
    """
    was_active = sink.voice_active
    sink.interval_ended(action, ts_ms)
    if was_active and not sink.voice_active and ctx.janitor is not None:
        ctx.janitor.release_hotkey(ctx.bindings.voice)
    return False
