"""X11 XGrabKey 全局快捷键（X11 独占，支持 voice / screenshot / ocr / repeat 四键）。

首个 KeyPress 即一次按下的开始并放行；trigger 松开（50ms 内无同键 KeyPress，滤掉 X 自动重复的松开/按下对）即结束。
XGrabKey 收不到修饰键松开，所以 X11 下「任一键松开即结束」不成立（第七节已知限制）。停录后补发修饰键抬起是
本后端的现状语义，经 end_interval_and_release 完成。
"""
from __future__ import annotations

import sys

from _linux.hotkey_actions import GLib, HotkeyContext, HotkeySink, end_interval_and_release
from _linux.hotkey_state import Action

# 标准 X11 常量，避免在模块导入阶段强依赖 python-xlib（隔离单测可注入假 Display）。
_X_KEY_PRESS = 2
_X_KEY_RELEASE = 3
_X_GRAB_MODE_ASYNC = 1
_DEFAULT_LOCK_MASKS = (0, 1 << 4, 1 << 1, (1 << 4) | (1 << 1))  # 0, Mod2Mask, LockMask, both
_AUTOREPEAT_RELEASE_MS = 50
_MASK = 0xFFFFFFFF


class HotkeyGrabber:
    """XGrabKey 快捷键管理，通过 GLib.io_add_watch 集成到事件循环。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        sink: HotkeySink,
        *,
        display_factory=None,
        hotkey_parser=None,
    ) -> None:
        self.ctx = ctx
        self.sink = sink
        self._pending_release: dict[int, int] = {}  # trigger 键码 → 待定的松开计时器
        if display_factory is None:
            from Xlib import display as xdisplay

            display_factory = xdisplay.Display
        if hotkey_parser is None:
            from _linux.x11keys import LOCK_MASKS, parse_hotkey

            hotkey_parser = parse_hotkey
            self._lock_masks = tuple(LOCK_MASKS)
        else:
            self._lock_masks = _DEFAULT_LOCK_MASKS

        self.dpy = display_factory()
        self._grab_errors: list[str] = []
        self._reported_grab_error = False
        self.dpy.set_error_handler(self._on_x_error)
        self.root = self.dpy.screen().root

        v_mask, v_sym = hotkey_parser(self.ctx.bindings.voice)
        s_mask, s_sym = hotkey_parser(self.ctx.bindings.screenshot)
        o_mask, o_sym = (
            hotkey_parser(self.ctx.bindings.ocr) if self.ctx.bindings.ocr else (0, 0)
        )
        r_mask, r_sym = hotkey_parser(self.ctx.bindings.repeat)

        self.voice_kc = self.dpy.keysym_to_keycode(v_sym)
        self.voice_mask = v_mask
        self.screen_kc = self.dpy.keysym_to_keycode(s_sym)
        self.screen_mask = s_mask
        self.ocr_kc = self.dpy.keysym_to_keycode(o_sym) if o_sym else 0
        self.ocr_mask = o_mask
        self.repeat_kc = self.dpy.keysym_to_keycode(r_sym)
        self.repeat_mask = r_mask
        self._actions_by_kc: dict[int, Action] = {
            kc: action
            for kc, action in (
                (self.voice_kc, "voice"),
                (self.screen_kc, "screenshot"),
                (self.ocr_kc, "ocr"),
                (self.repeat_kc, "repeat"),
            )
            if kc
        }

    def _on_x_error(self, err, *args):
        del args
        try:
            self._grab_errors.append(str(err))
        except Exception:
            pass

    def _key_specs(self) -> list[tuple[int, int]]:
        specs = [
            (self.voice_kc, self.voice_mask),
            (self.screen_kc, self.screen_mask),
            (self.repeat_kc, self.repeat_mask),
        ]
        if self.ocr_kc:
            specs.append((self.ocr_kc, self.ocr_mask))
        return specs

    def grab(self) -> None:
        for lock in self._lock_masks:
            for kc, mask in self._key_specs():
                self.root.grab_key(
                    kc,
                    mask | lock,
                    True,
                    _X_GRAB_MODE_ASYNC,
                    _X_GRAB_MODE_ASYNC,
                )
        self.dpy.flush()
        try:
            self.dpy.sync()
        except Exception:
            pass
        if self._grab_errors:
            if not self._reported_grab_error:
                self._reported_grab_error = True
                for error in self._grab_errors:
                    print(f"  快捷键: XGrabKey 冲突/错误: {error}", flush=True)
            self._grab_errors.clear()

    def ungrab(self) -> None:
        for lock in self._lock_masks:
            for kc, mask in self._key_specs():
                self.root.ungrab_key(kc, mask | lock)
        self.dpy.flush()

    def start(self) -> None:
        self.grab()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)
        GLib.timeout_add(500, self._refresh_grab)

    def _refresh_grab(self) -> bool:
        if not self.sink.voice_active:
            try:
                self.grab()
            except Exception:
                pass
        return True

    def _handle_event(self, event) -> None:
        action = self._actions_by_kc.get(event.detail)
        if action is None:
            return
        t_ms = int(event.time) & _MASK
        if event.type == _X_KEY_PRESS:
            pending = self._pending_release.pop(event.detail, 0)
            if pending:
                GLib.source_remove(pending)  # X 自动重复的松开/按下对：仍是同一次按下
                return
            self.sink.interval_started(action, t_ms)
            self.sink.activated(action, t_ms)
        elif event.type == _X_KEY_RELEASE:
            previous = self._pending_release.pop(event.detail, 0)
            if previous:
                GLib.source_remove(previous)
            self._pending_release[event.detail] = GLib.timeout_add(
                _AUTOREPEAT_RELEASE_MS, self._release_after_autorepeat_window, event.detail, action, t_ms
            )

    def _release_after_autorepeat_window(self, keycode: int, action: Action, t_ms: int) -> bool:
        self._pending_release.pop(keycode, None)
        return end_interval_and_release(self.sink, self.ctx, action, t_ms)

    def _on_xlib_event(self, fd, condition) -> bool:
        del fd, condition
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XGrabKey sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                event = self.dpy.next_event()
                self._handle_event(event)
            except Exception as e:
                print(f"XGrabKey event error: {e}", file=sys.stderr)
        return True

    def cleanup(self) -> None:
        for source_id in self._pending_release.values():
            GLib.source_remove(source_id)
        self._pending_release.clear()
