"""快捷键后端共享的动作层：录音开始/停止、截图翻译、纯 OCR、重复输入、关闭浮层。

后端只负责把"按键按下/松开"翻译成这里的调用，不复制业务逻辑；业务实现由 `_desktop` 或 `_platform` 注入。
portal 路径已经有原生 Deactivated，无需补发修饰键释放。
当运行在无 `gi`（PyGObject）的隔离测试或纯 Qt 环境时，自动回退线程安全的定时器/即时调度。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from _linux.inject_guard import InjectGuard  # noqa: E402
from _linux.modifiers import ModifierJanitor  # noqa: E402


class HotkeyActions(Protocol):
    def voice_start(self) -> bool: ...

    def voice_stop(self) -> None: ...

    def screenshot(self) -> None: ...

    def repeat(self) -> None: ...

    def overlay_active(self) -> bool: ...

    def close_overlay(self) -> bool: ...


@dataclass
class CallbackHotkeyActions:
    """基于函数回调的 `HotkeyActions` 实现，完整支持四键（`on_ocr` 默认空回调兼容旧调用方）。"""

    on_voice_start: Callable[[], bool]
    on_voice_stop: Callable[[], None]
    on_screenshot: Callable[[], None]
    on_repeat: Callable[[], None] = lambda: None
    on_ocr: Callable[[], None] = lambda: None
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

    def overlay_active(self) -> bool:
        return self.is_overlay_active()

    def close_overlay(self) -> bool:
        return self.on_close_overlay()


def _noop() -> None:
    return None


@dataclass
class HotkeyContext:
    actions: HotkeyActions
    bindings: HotkeyBindings
    janitor: ModifierJanitor
    guard: InjectGuard
    session_type: str
    on_ocr: Callable[[], None] = field(default=_noop)


class HotkeyActionMixin:
    ctx: HotkeyContext
    voice_active: bool
    voice_started_at: float
    _pending_release_id: int
    _release_modifiers_on_stop = True

    def _init_hotkey_actions(self, ctx: HotkeyContext) -> None:
        self.ctx = ctx
        self.voice_active = False
        self.voice_started_at = 0.0  # 录音开始时刻，供看门狗判定 release 丢失卡死
        self._pending_release_id = 0

    def _cancel_pending_release(self) -> None:
        if self._pending_release_id:
            try:
                GLib.source_remove(self._pending_release_id)
            except Exception:
                pass
            self._pending_release_id = 0

    def reset_voice(self) -> None:
        """外部看门狗强制 finish 后复位录音按下态并取消挂起释放定时器。"""
        self._cancel_pending_release()
        self.voice_active = False
        self.voice_started_at = 0.0

    def _on_voice_press(self) -> None:
        self._cancel_pending_release()
        if not self.voice_active:
            self.voice_active = True
            self.voice_started_at = time.monotonic()
            if not self.ctx.actions.voice_start():
                self.voice_active = False

    def _schedule_release(self) -> bool:
        """50ms 延迟释放：X11 自动重复会在 KeyRelease 后立刻补 KeyPress。"""
        self._cancel_pending_release()
        self._pending_release_id = GLib.timeout_add(50, self._do_release)
        return False

    def _do_release(self) -> bool:
        self._pending_release_id = 0
        if self.voice_active:
            self.voice_active = False
            self.ctx.actions.voice_stop()
            if self._release_modifiers_on_stop and self.ctx.janitor is not None:
                self.ctx.janitor.release_hotkey(self.ctx.bindings.voice)
        return False

    def _on_screenshot(self) -> bool:
        self.ctx.actions.screenshot()
        return False

    def _on_ocr(self) -> bool:
        ocr_fn = getattr(self.ctx.actions, "ocr", None)
        if callable(ocr_fn):
            ocr_fn()
        else:
            self.ctx.on_ocr()
        return False

    def _on_repeat(self) -> bool:
        self.ctx.actions.repeat()
        return False

    def _close_overlay_on_key(self) -> None:
        """非修饰键且未命中自身热键的普通按键（含 Esc）→ 关活跃浮窗，不依赖焦点。"""
        if self.ctx.actions.overlay_active():
            GLib.idle_add(self.ctx.actions.close_overlay)
