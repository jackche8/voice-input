"""跨平台 `pynput` 四键全局热键状态机（Windows / macOS 共用唯一实现，含 `voice` 按住/松开）。"""
from __future__ import annotations

import threading
from collections.abc import Callable

from _hotkey import HotkeyBindings
from _platform._common import always_false, noop


def _default_is_valid_code(code: int) -> bool:
    return code >= 0


class PynputHotkeyRig:
    """统一 `pynput` 四键监听器状态机。

    平台差异仅通过注入 `backend_name`、`hotkey_parser`、`key_extractor` 与 `is_valid_code` 表达，
    不在 Windows / macOS 适配器里各自复制一套按键状态机。
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
        overlay_active: Callable[[], bool] = always_false,
        close_overlay: Callable[[], bool] = always_false,
        listener_factory: Callable[..., object] | None = None,
    ) -> None:
        self.backend_name = backend_name
        self._key_extractor = key_extractor
        self._is_valid_code = is_valid_code

        self.voice_spec = hotkey_parser(bindings.voice)
        self.screenshot_spec = hotkey_parser(bindings.screenshot)
        self.ocr_spec = hotkey_parser(bindings.ocr) if bindings.ocr else None
        self.repeat_spec = hotkey_parser(bindings.repeat)

        self._on_voice_start = on_voice_start
        self._on_voice_stop = on_voice_stop
        self._on_screenshot = on_screenshot
        self._on_ocr = on_ocr
        self._on_repeat = on_repeat
        self._overlay_active = overlay_active
        self._close_overlay = close_overlay
        self._listener_factory = listener_factory

        self._lock = threading.Lock()
        self._held_mod_tokens: set[tuple[int, int]] = set()
        self._pressed_codes: set[int] = set()
        self.voice_active = False
        self._listener = None

    def _current_mod_mask(self) -> int:
        mask = 0
        for flag, _code in self._held_mod_tokens:
            mask |= flag
        return mask

    def handle_press(self, key) -> None:
        mod_flag, code = self._key_extractor(key)
        valid_code = self._is_valid_code(code)
        action_to_run: Callable[[], object] | None = None
        should_close_overlay = False

        with self._lock:
            if mod_flag:
                token = (mod_flag, code if valid_code else mod_flag)
                self._held_mod_tokens.add(token)
            elif valid_code:
                if code in self._pressed_codes:
                    return
                self._pressed_codes.add(code)
                mods = self._current_mod_mask()
                matched_binding = False
                if (mods, code) == self.voice_spec:
                    matched_binding = True
                    if not self.voice_active:
                        self.voice_active = True
                        action_to_run = self._start_voice_guarded
                elif (mods, code) == self.screenshot_spec:
                    matched_binding = True
                    action_to_run = self._on_screenshot
                elif self.ocr_spec is not None and (mods, code) == self.ocr_spec:
                    matched_binding = True
                    action_to_run = self._on_ocr
                elif (mods, code) == self.repeat_spec:
                    matched_binding = True
                    action_to_run = self._on_repeat
                if not matched_binding and self._overlay_active():
                    should_close_overlay = True

        if should_close_overlay:
            self._close_overlay()
        if action_to_run is not None:
            action_to_run()

    def _start_voice_guarded(self) -> None:
        if not self._on_voice_start():
            with self._lock:
                self.voice_active = False

    def handle_release(self, key) -> None:
        mod_flag, code = self._key_extractor(key)
        valid_code = self._is_valid_code(code)
        stop_voice = False

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
                if (valid_code and code == v_code) or (mods_now & v_mods) != v_mods:
                    self.voice_active = False
                    stop_voice = True

        if stop_voice:
            self._on_voice_stop()

    def reset_voice(self) -> None:
        """外部看门狗强制 finish 后复位按住说话状态机与可能丢失松开事件的触发键/修饰键。"""
        with self._lock:
            self.voice_active = False
            v_mods, v_code = self.voice_spec
            self._pressed_codes.discard(v_code)
            if v_mods:
                self._held_mod_tokens = {
                    item for item in self._held_mod_tokens if not (item[0] & v_mods)
                }

    def start(self) -> None:
        if self._listener is not None:
            return
        if self._listener_factory is not None:
            listener = self._listener_factory(
                on_press=self.handle_press, on_release=self.handle_release
            )
        else:
            from pynput import keyboard

            listener = keyboard.Listener(
                on_press=self.handle_press, on_release=self.handle_release
            )
        listener.start()
        self._listener = listener

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            stop_fn = getattr(listener, "stop", None)
            if callable(stop_fn):
                stop_fn()
