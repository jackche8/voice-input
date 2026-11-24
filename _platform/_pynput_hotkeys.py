"""跨平台 `pynput` 五键全局热键状态机（Windows / macOS 共用唯一实现，含吞键拦截与异步串行派发）。"""
from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable

from _hotkey import HotkeyBindings
from _platform._common import always_false, noop

RESIDUAL_SUPPRESS_WINDOW_S = 1.5
_ACTION_STOP_TIMEOUT_S = 2.0


def _default_is_valid_code(code: int) -> bool:
    return code >= 0


class PynputHotkeyRig:
    """统一 `pynput` 五键监听器状态机。

    平台差异仅通过注入 `backend_name`、`hotkey_parser`、`key_extractor`、`is_valid_code`、
    `modifier_state_reader` 与 `_listener_kwargs()` 表达，不在 Windows / macOS 适配器里复制第二套状态机。
    """

    def __init__(
        self,
        bindings: HotkeyBindings,
        *,
        backend_name: str,
        hotkey_parser: Callable[[str], tuple[int, int]],
        key_extractor: Callable[[object], tuple[int, int]],
        is_valid_code: Callable[[int], bool] = _default_is_valid_code,
        on_voice_start: Callable[[], bool],
        on_voice_stop: Callable[[], None],
        on_screenshot: Callable[[], None],
        on_ocr: Callable[[], None] = noop,
        on_repeat: Callable[[], None] = noop,
        on_annotate: Callable[[], None] = noop,
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
        listener_factory: Callable[..., object] | None = None,
        modifier_state_reader: Callable[[], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend_name = backend_name
        self._key_extractor = key_extractor
        self._is_valid_code = is_valid_code
        self._modifier_state_reader = modifier_state_reader
        self._clock = clock

        self.voice_spec = hotkey_parser(bindings.voice)
        self.screenshot_spec = hotkey_parser(bindings.screenshot)
        self.ocr_spec = hotkey_parser(bindings.ocr) if bindings.ocr else None
        self.repeat_spec = hotkey_parser(bindings.repeat)
        self.annotate_spec = hotkey_parser(bindings.annotate) if bindings.annotate else None
        self._escape_code = hotkey_parser("escape")[1]  # 浮窗只认 Esc 关闭（与 Linux evdev 一致）

        self._on_voice_start = on_voice_start
        self._on_voice_stop = on_voice_stop
        self._on_screenshot = on_screenshot
        self._on_ocr = on_ocr
        self._on_repeat = on_repeat
        self._on_annotate = on_annotate
        self._overlay_active = overlay_active
        self._close_overlay = close_overlay
        self._listener_factory = listener_factory

        self._lock = threading.Lock()
        self._held_mod_tokens: set[tuple[int, int]] = set()
        self._pressed_codes: set[int] = set()
        self._suppressed_codes: dict[int, float] = {}
        self._voice_generation = 0
        self._last_suppress = False
        self.voice_active = False
        self._listener = None
        self._action_queue: queue.Queue[Callable[[], object] | None] | None = None
        self._action_thread: threading.Thread | None = None

    def _current_mod_mask(self) -> int:
        mask = 0
        for flag, _code in self._held_mod_tokens:
            mask |= flag
        return mask

    def _listener_kwargs(self) -> dict[str, object]:
        return {}

    def _dispatch_action(self, fn: Callable[[], object]) -> None:
        q = self._action_queue
        if q is not None:
            q.put(fn)
            return
        fn()

    def _action_worker(self, q: queue.Queue[Callable[[], object] | None]) -> None:
        while True:
            item = q.get()
            if item is None:
                return
            try:
                item()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def _stop_worker(self) -> None:
        q = self._action_queue
        worker = self._action_thread
        self._action_queue = None
        self._action_thread = None
        if q is not None:
            q.put(None)
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=_ACTION_STOP_TIMEOUT_S)

    def drain_actions(self, timeout: float = _ACTION_STOP_TIMEOUT_S) -> None:
        """等待异步动作队列中此前已入队的回调全部执行完毕（未启动 worker 时立即返回）。"""
        q = self._action_queue
        if q is None:
            return
        done = threading.Event()
        q.put(done.set)
        if not done.wait(timeout):
            raise TimeoutError(f"等待热键动作队列空闲超时 ({timeout}s)")

    def _consume_last_suppress(self) -> bool:
        with self._lock:
            suppress = self._last_suppress
            self._last_suppress = False
            return suppress

    def _handle_press_raw(self, mod_flag: int, code: int) -> bool:
        valid_code = self._is_valid_code(code)
        real_mask: int | None = None
        if not mod_flag and valid_code and self._modifier_state_reader is not None:
            real_mask = self._modifier_state_reader()

        actions_to_run: list[Callable[[], object]] = []
        should_suppress = False
        now = self._clock()

        with self._lock:
            if mod_flag:
                token = (mod_flag, code if valid_code else mod_flag)
                self._held_mod_tokens.add(token)
                self._last_suppress = False
                return False

            if not valid_code:
                self._last_suppress = False
                return False

            if real_mask is not None:
                # 仅裁剪已失效的过期修饰键 token，严禁插入合成 token
                self._held_mod_tokens = {
                    item for item in self._held_mod_tokens if item[0] & real_mask
                }
                v_mods, v_code = self.voice_spec
                if self.voice_active and (real_mask & v_mods) != v_mods:
                    self.voice_active = False
                    self._voice_generation += 1
                    self._suppressed_codes.clear()
                    self._pressed_codes.discard(v_code)
                    actions_to_run.append(self._on_voice_stop)
                mods = real_mask
            else:
                mods = self._current_mod_mask()

            if code in self._pressed_codes:
                suppressed_ts = self._suppressed_codes.get(code)
                if suppressed_ts is not None:
                    if self.voice_active and code == self.voice_spec[1]:
                        self._suppressed_codes[code] = now
                        should_suppress = True
                    elif now - suppressed_ts <= RESIDUAL_SUPPRESS_WINDOW_S:
                        should_suppress = True
                    else:
                        # 超过系统连发窗口自动解除，防丢失 KEYUP 造成永久吞键
                        self._suppressed_codes.pop(code, None)
                        self._pressed_codes.discard(code)

            if code in self._pressed_codes:
                self._last_suppress = should_suppress
            else:
                self._pressed_codes.add(code)
                matched_binding = False
                if (mods, code) == self.voice_spec:
                    matched_binding = True
                    should_suppress = True
                    self._suppressed_codes[code] = now
                    if not self.voice_active:
                        self.voice_active = True
                        self._voice_generation += 1
                        gen = self._voice_generation
                        actions_to_run.append(lambda g=gen: self._start_voice_guarded(g))
                elif (mods, code) == self.screenshot_spec:
                    matched_binding = True
                    should_suppress = True
                    self._suppressed_codes[code] = now
                    actions_to_run.append(self._on_screenshot)
                elif self.ocr_spec is not None and (mods, code) == self.ocr_spec:
                    matched_binding = True
                    should_suppress = True
                    self._suppressed_codes[code] = now
                    actions_to_run.append(self._on_ocr)
                elif (mods, code) == self.repeat_spec:
                    matched_binding = True
                    should_suppress = True
                    self._suppressed_codes[code] = now
                    actions_to_run.append(self._on_repeat)
                elif self.annotate_spec is not None and (mods, code) == self.annotate_spec:
                    matched_binding = True
                    should_suppress = True
                    self._suppressed_codes[code] = now
                    actions_to_run.append(self._on_annotate)

                if not matched_binding:
                    self._suppressed_codes.pop(code, None)
                    # Option A：浮窗激活态 Esc 不吞键（保留聚焦态 Qt keyPressEvent），仅异步调 close_overlay()
                    if code == self._escape_code and self._overlay_active():
                        actions_to_run.append(self._close_overlay)

                self._last_suppress = should_suppress

        for action in actions_to_run:
            self._dispatch_action(action)
        return should_suppress

    def _start_voice_guarded(self, gen: int) -> None:
        started = False
        try:
            started = bool(self._on_voice_start())
        finally:
            if not started:
                now = self._clock()
                with self._lock:
                    if self._voice_generation == gen:
                        self.voice_active = False
                        v_code = self.voice_spec[1]
                        if v_code in self._suppressed_codes:
                            self._suppressed_codes[v_code] = now

    def _handle_release_raw(self, mod_flag: int, code: int) -> bool:
        valid_code = self._is_valid_code(code)
        stop_voice = False
        should_suppress = False
        now = self._clock()

        with self._lock:
            if mod_flag:
                if valid_code:
                    self._held_mod_tokens.discard((mod_flag, code))
                else:
                    self._held_mod_tokens = {
                        item for item in self._held_mod_tokens if item[0] != mod_flag
                    }
            if valid_code:
                self._pressed_codes.discard(code)

            if self.voice_active:
                v_mods, v_code = self.voice_spec
                mods_now = self._current_mod_mask()
                trigger_released = valid_code and code == v_code
                if trigger_released or (mods_now & v_mods) != v_mods:
                    self.voice_active = False
                    self._voice_generation += 1
                    stop_voice = True
                    if not trigger_released and v_code in self._suppressed_codes:
                        # 修饰键先松开：从语音停止时刻起算 1.5s 残留连发吞键窗口
                        self._suppressed_codes[v_code] = now

            if not mod_flag and valid_code and code in self._suppressed_codes:
                self._suppressed_codes.pop(code, None)
                should_suppress = True

            self._last_suppress = should_suppress

        if stop_voice:
            self._dispatch_action(self._on_voice_stop)
        return should_suppress

    def handle_press(self, key, injected: bool = False) -> None:
        if injected:
            with self._lock:
                self._last_suppress = False
            return None
        mod_flag, code = self._key_extractor(key)
        self._handle_press_raw(mod_flag, code)
        return None

    def handle_release(self, key, injected: bool = False) -> None:
        if injected:
            with self._lock:
                self._last_suppress = False
            return None
        mod_flag, code = self._key_extractor(key)
        self._handle_release_raw(mod_flag, code)
        return None

    def reset_voice(self) -> None:
        """外部看门狗强制 finish 后复位按住说话状态机与可能丢失松开事件的触发键/修饰键。"""
        with self._lock:
            self.voice_active = False
            self._voice_generation += 1
            v_mods, v_code = self.voice_spec
            self._pressed_codes.discard(v_code)
            self._suppressed_codes.pop(v_code, None)
            if v_mods:
                self._held_mod_tokens = {
                    item for item in self._held_mod_tokens if not (item[0] & v_mods)
                }

    def start(self) -> None:
        if self._listener is not None:
            return
        action_queue: queue.Queue[Callable[[], object] | None] = queue.Queue()
        worker = threading.Thread(
            target=self._action_worker,
            args=(action_queue,),
            name=f"{self.backend_name}-actions",
            daemon=True,
        )
        worker.start()
        self._action_queue = action_queue
        self._action_thread = worker

        kwargs: dict[str, object] = {
            "on_press": self.handle_press,
            "on_release": self.handle_release,
            **self._listener_kwargs(),
        }
        try:
            if self._listener_factory is not None:
                listener = self._listener_factory(**kwargs)
            else:
                from pynput import keyboard

                listener = keyboard.Listener(**kwargs)
            self._listener = listener
            listener.start()
        except Exception:
            self._listener = None
            self._stop_worker()
            raise

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            stop_fn = getattr(listener, "stop", None)
            if callable(stop_fn):
                stop_fn()
        self._stop_worker()
        with self._lock:
            self.voice_active = False
            self._voice_generation += 1
            self._held_mod_tokens.clear()
            self._pressed_codes.clear()
            self._suppressed_codes.clear()
            self._last_suppress = False
