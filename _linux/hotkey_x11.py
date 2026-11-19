"""X11 XGrabKey 全局快捷键（X11 独占，支持 voice / screenshot / ocr / repeat 四键）。"""
from __future__ import annotations

import sys

from _linux.hotkey_actions import GLib, HotkeyActionMixin, HotkeyContext

# 标准 X11 常量，避免在模块导入阶段强依赖 python-xlib（隔离单测可注入假 Display）。
_X_KEY_PRESS = 2
_X_KEY_RELEASE = 3
_X_GRAB_MODE_ASYNC = 1
_DEFAULT_LOCK_MASKS = (0, 1 << 4, 1 << 1, (1 << 4) | (1 << 1))  # 0, Mod2Mask, LockMask, both


class HotkeyGrabber(HotkeyActionMixin):
    """XGrabKey 快捷键管理，通过 GLib.io_add_watch 集成到事件循环。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        *,
        display_factory=None,
        hotkey_parser=None,
    ) -> None:
        self._init_hotkey_actions(ctx)
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
        if not self.voice_active:
            try:
                self.grab()
            except Exception:
                pass
        return True

    def _handle_event(self, event) -> None:
        if event.type == _X_KEY_PRESS:
            if event.detail == self.voice_kc:
                self._on_voice_press()
            elif event.detail == self.screen_kc:
                self._on_screenshot()
            elif self.ocr_kc and event.detail == self.ocr_kc:
                self._on_ocr()
            elif event.detail == self.repeat_kc:
                self._on_repeat()
        elif event.type == _X_KEY_RELEASE:
            if event.detail == self.voice_kc and self.voice_active:
                self._schedule_release()

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
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
