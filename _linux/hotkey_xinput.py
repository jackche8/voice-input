"""XInput2 RawKey 全局快捷键 (X server 客户端层 raw event stream，支持四键)。"""
from __future__ import annotations

import sys
import time

from _linux.hotkey_actions import GLib, HotkeyActionMixin, HotkeyContext
from _linux.uinput_device import UINPUT_DEVICE_NAME

STUCK_MOD_HARD_SECONDS = 6.0
STUCK_MOD_SOFT_SECONDS = 2.5
STUCK_MOD_IDLE_SECONDS = 1.5
STUCK_MOD_KEYPRESS_SECONDS = 3.0
STUCK_MOD_MATCH_SECONDS = 3.0

# X11 / XInput2 协议常量（与 Xlib.X / Xlib.ext.xinput 一致，供无 Xlib 隔离环境复用）
_X_MAPPING_NOTIFY = 34
_X_SHIFT_MASK = 1 << 0
_X_CONTROL_MASK = 1 << 2
_X_MOD1_MASK = 1 << 3
_X_MOD4_MASK = 1 << 6
_XI_ALL_DEVICES = 0
_XI_ALL_MASTER_DEVICES = 1
_XI_RAW_KEY_PRESS = 13
_XI_RAW_KEY_RELEASE = 14
_XI_RAW_KEY_PRESS_MASK = 1 << _XI_RAW_KEY_PRESS
_XI_RAW_KEY_RELEASE_MASK = 1 << _XI_RAW_KEY_RELEASE


def xi_source_ids_by_name(dpy, name: str) -> set[int]:
    """查出 XInput2 中指定设备名的全部 deviceid（slave sourceid）。"""
    ids: set[int] = set()
    try:
        reply = dpy.xinput_query_device(_XI_ALL_DEVICES)
    except Exception:
        return ids
    for info in getattr(reply, "devices", None) or ():
        if getattr(info, "name", None) != name:
            continue
        did = getattr(info, "deviceid", None)
        if did:
            ids.add(int(did))
    return ids


def _xi_source_ids_matching(dpy, needle: str) -> set[int]:
    """查出 XInput2 中设备名包含 needle 的全部 deviceid（XTEST 等）。"""
    ids: set[int] = set()
    try:
        reply = dpy.xinput_query_device(_XI_ALL_DEVICES)
    except Exception:
        return ids
    for info in getattr(reply, "devices", None) or ():
        name = getattr(info, "name", None) or ""
        if needle.lower() not in name.lower():
            continue
        did = getattr(info, "deviceid", None)
        if did:
            ids.add(int(did))
    return ids


def _build_xi_raw_event_struct():
    from Xlib.protocol import rq as xrq

    return xrq.Struct(
        xrq.Card16("deviceid"),
        xrq.Card32("time"),
        xrq.Card32("detail"),
        xrq.Card16("sourceid"),
        xrq.Card16("valuators_len"),
        xrq.Card32("flags"),
        xrq.Pad(4),
    )


class XInputHotkeyGrabber(HotkeyActionMixin):
    """XInput2 RawKey 全局快捷键管理，完整支持 voice / screenshot / ocr / repeat 四键。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        *,
        display_factory=None,
        hotkey_parser=None,
        keysym_resolver=None,
    ) -> None:
        self._init_hotkey_actions(ctx)
        self.actions_enabled = True
        self.only_xtest_sources = False
        self._pressed: set[int] = set()
        self._ignore_source_ids: set[int] = set()
        self._xtest_source_ids: set[int] = set()

        if display_factory is None:
            from Xlib import XK
            from Xlib import display as xdisplay
            from Xlib.ext import xinput as xi_ext

            display_factory = xdisplay.Display
            ext_name = xi_ext.extname
            raw_struct = _build_xi_raw_event_struct()
            if keysym_resolver is None:
                keysym_resolver = XK.string_to_keysym
        else:
            ext_name = "XInputExtension"
            raw_struct = None
            if keysym_resolver is None:
                _fake_syms = {
                    "Control_L": 0xFFE3,
                    "Control_R": 0xFFE4,
                    "Shift_L": 0xFFE1,
                    "Shift_R": 0xFFE2,
                    "Alt_L": 0xFFE9,
                    "Alt_R": 0xFFEA,
                    "Super_L": 0xFFEB,
                    "Super_R": 0xFFEC,
                    "Meta_L": 0xFFE7,
                    "Meta_R": 0xFFE8,
                }
                keysym_resolver = lambda name: _fake_syms.get(name, 0)

        if hotkey_parser is None:
            from _linux.x11keys import parse_hotkey

            hotkey_parser = parse_hotkey

        self._hotkey_parser = hotkey_parser
        self._keysym_resolver = keysym_resolver

        self.dpy = display_factory()
        self.dpy.set_error_handler(self._ignore_error)
        if not self.dpy.has_extension("XInputExtension"):
            self.dpy.close()
            raise RuntimeError("X server 未启用 XInputExtension")

        self._major = self.dpy.display.get_extension_major(ext_name)
        qv = self.dpy.xinput_query_version()
        if qv.major_version < 2:
            self.dpy.close()
            raise RuntimeError(
                f"XInput 版本过低 ({qv.major_version}.{qv.minor_version}), 需要 >= 2.0"
            )

        if raw_struct is not None:
            try:
                self.dpy.ge_add_event_data(self._major, _XI_RAW_KEY_PRESS, raw_struct)
                self.dpy.ge_add_event_data(self._major, _XI_RAW_KEY_RELEASE, raw_struct)
            except Exception as exc:
                self.dpy.close()
                raise RuntimeError(f"无法注册 XInput2 RawKey parser: {exc}") from exc

        self.root = self.dpy.screen().root

        self._mod_kcs: set[int] = set()
        for _nm in (
            "Control_L",
            "Control_R",
            "Shift_L",
            "Shift_R",
            "Alt_L",
            "Alt_R",
            "Super_L",
            "Super_R",
            "Meta_L",
            "Meta_R",
        ):
            _ks = self._keysym_resolver(_nm)
            if _ks:
                _kc = self.dpy.keysym_to_keycode(_ks)
                if _kc:
                    self._mod_kcs.add(_kc)
        self._mod_down_since: dict[int, float] = {}
        self._last_key_activity = time.monotonic()
        self._last_stuck_check = 0.0

        self._voice_combo = self._compile_hotkey(self.ctx.bindings.voice)
        self._screen_combo = self._compile_hotkey(self.ctx.bindings.screenshot)
        self._ocr_combo = (
            self._compile_hotkey(self.ctx.bindings.ocr)
            if self.ctx.bindings.ocr
            else None
        )
        self._repeat_combo = self._compile_hotkey(self.ctx.bindings.repeat)
        print(
            f"  XInput2: 语音 {self.ctx.bindings.voice} "
            f"→ trigger=kc{self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}"
        )
        print(
            f"  XInput2: 截图 {self.ctx.bindings.screenshot} "
            f"→ trigger=kc{self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}"
        )
        if self._ocr_combo is not None:
            print(
                f"  XInput2: OCR {self.ctx.bindings.ocr} "
                f"→ trigger=kc{self._ocr_combo['trigger']} mods={self._ocr_combo['modifiers']}"
            )
        print(
            f"  XInput2: 重复 {self.ctx.bindings.repeat} "
            f"→ trigger=kc{self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}"
        )

    def _compile_hotkey(self, hotkey_str: str) -> dict:
        """解析快捷键字符串 → {'trigger': keycode, 'modifiers': [set, ...]} (X11 keycode)."""
        mask, keysym = self._hotkey_parser(hotkey_str)
        trigger_kc = self.dpy.keysym_to_keycode(keysym)
        if not trigger_kc:
            raise ValueError(f"无法解析快捷键 {hotkey_str} 的触发键 keysym=0x{keysym:x}")
        modifiers = []
        if mask & _X_CONTROL_MASK:
            modifiers.append(self._kc_names(["Control_L", "Control_R"]))
        if mask & _X_SHIFT_MASK:
            modifiers.append(self._kc_names(["Shift_L", "Shift_R"]))
        if mask & _X_MOD1_MASK:
            modifiers.append(self._kc_names(["Alt_L", "Alt_R", "Meta_L", "Meta_R"]))
        if mask & _X_MOD4_MASK:
            modifiers.append(self._kc_names(["Super_L", "Super_R"]))
        return {"trigger": trigger_kc, "modifiers": modifiers}

    def _kc_names(self, sym_names: list[str]) -> set[int]:
        out: set[int] = set()
        for name in sym_names:
            ks = self._keysym_resolver(name)
            if ks:
                kc = self.dpy.keysym_to_keycode(ks)
                if kc:
                    out.add(kc)
        return out

    @staticmethod
    def _ignore_error(err, *args):
        del err, args

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass

    def start(self) -> None:
        mask = _XI_RAW_KEY_PRESS_MASK | _XI_RAW_KEY_RELEASE_MASK
        self.root.xinput_select_events([(_XI_ALL_MASTER_DEVICES, mask)])
        self.dpy.flush()
        self._refresh_ignore_sources()
        GLib.io_add_watch(self.dpy.fileno(), GLib.IO_IN, self._on_xlib_event)

    def _refresh_ignore_sources(self) -> None:
        self._ignore_source_ids = xi_source_ids_by_name(self.dpy, UINPUT_DEVICE_NAME)
        self._xtest_source_ids = _xi_source_ids_matching(self.dpy, "XTEST")

    def _is_injected_source(self, sourceid: int) -> bool:
        return bool(sourceid) and sourceid in self._ignore_source_ids

    def _is_xtest_source(self, sourceid: int) -> bool:
        return bool(sourceid) and sourceid in self._xtest_source_ids

    def _on_xlib_event(self, fd, condition) -> bool:
        del fd, condition
        try:
            self.dpy.sync()
        except Exception as e:
            print(f"XInput2 sync error: {e}", file=sys.stderr)
            return True
        while self.dpy.pending_events() > 0:
            try:
                e = self.dpy.next_event()
                if getattr(e, "type", None) == _X_MAPPING_NOTIFY:
                    self._refresh_ignore_sources()
                    continue
                if getattr(e, "extension", None) != self._major:
                    continue
                evt = getattr(e, "evtype", None)
                if evt not in (_XI_RAW_KEY_PRESS, _XI_RAW_KEY_RELEASE):
                    continue
                if self._is_injected_source(getattr(e.data, "sourceid", 0)):
                    continue
                sourceid = int(getattr(e.data, "sourceid", 0))
                if evt == _XI_RAW_KEY_PRESS:
                    self._handle_press(e.data.detail, sourceid)
                else:
                    self._handle_release(e.data.detail, sourceid)
            except Exception as e:
                print(f"XInput2 event error: {e}", file=sys.stderr)
        return True

    def _handle_press(self, keycode: int, sourceid: int = 0) -> None:
        if not self._ignore_source_ids and self.ctx.guard.active():
            return
        self._last_key_activity = time.monotonic()
        if keycode in self._mod_kcs:
            self._mod_down_since.setdefault(keycode, self._last_key_activity)
        elif not self.voice_active and self._last_key_activity - self._last_stuck_check > 0.25:
            self._last_stuck_check = self._last_key_activity
            if self._has_lingering_modifier(STUCK_MOD_KEYPRESS_SECONDS):
                lingering = self._lingering_modifiers()
                ghosts = {
                    kc: h
                    for kc, h in lingering.items()
                    if h > STUCK_MOD_KEYPRESS_SECONDS
                }
                print(
                    f"[stuck] 打字时检测到卡住的修饰键 {list(ghosts)}, 立即清理",
                    flush=True,
                )
                self.ctx.janitor.clear_xtest()
                for kc in ghosts:
                    self._pressed.discard(kc)
                    self._mod_down_since.pop(kc, None)
        if not self.actions_enabled:
            return
        if self.only_xtest_sources and not self._is_xtest_source(sourceid):
            return
        is_repeat = keycode in self._pressed
        self._pressed.add(keycode)
        if keycode == self._voice_combo["trigger"] and self.voice_active:
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
                self._pending_release_id = 0
            return
        if is_repeat:
            return
        if self._match_combo(self._voice_combo, keycode):
            self._on_voice_press()
        elif self._match_combo(self._screen_combo, keycode):
            GLib.idle_add(self._on_screenshot)
        elif self._ocr_combo is not None and self._match_combo(self._ocr_combo, keycode):
            GLib.idle_add(self._on_ocr)
        elif self._match_combo(self._repeat_combo, keycode):
            self._on_repeat()
        elif keycode not in self._mod_kcs:
            self._close_overlay_on_key()

    def _handle_release(self, keycode: int, sourceid: int = 0) -> None:
        if not self._ignore_source_ids and self.ctx.guard.active():
            triggers = {
                self._voice_combo["trigger"],
                self._screen_combo["trigger"],
                self._repeat_combo["trigger"],
            }
            if self._ocr_combo is not None:
                triggers.add(self._ocr_combo["trigger"])
            if keycode not in self._pressed or keycode not in triggers:
                return
        self._last_key_activity = time.monotonic()
        self._mod_down_since.pop(keycode, None)
        self._pressed.discard(keycode)
        if (
            self.voice_active
            and self._is_voice_combo_key(keycode)
            and self.actions_enabled
            and (not self.only_xtest_sources or self._is_xtest_source(sourceid))
        ):
            if self._pending_release_id:
                GLib.source_remove(self._pending_release_id)
            self._pending_release_id = GLib.timeout_add(50, self._do_release)

    def _is_voice_combo_key(self, keycode: int) -> bool:
        if keycode == self._voice_combo["trigger"]:
            return True
        for mod_set in self._voice_combo["modifiers"]:
            if keycode in mod_set:
                return True
        return False

    def _lingering_modifiers(self) -> dict[int, float]:
        now = time.monotonic()
        try:
            km = self.dpy.query_keymap()
        except Exception:
            return {}
        down_now = {kc for kc in self._mod_kcs if km[kc >> 3] & (1 << (kc & 7))}
        for kc in list(self._mod_down_since):
            if kc not in down_now:
                self._mod_down_since.pop(kc, None)
        for kc in down_now:
            self._mod_down_since.setdefault(kc, now)
        return {kc: now - self._mod_down_since[kc] for kc in down_now}

    def _has_lingering_modifier(self, min_held: float) -> bool:
        return any(h > min_held for h in self._lingering_modifiers().values())

    def has_stuck_modifiers(self) -> bool:
        idle = time.monotonic() - self._last_key_activity
        for held in self._lingering_modifiers().values():
            if held > STUCK_MOD_HARD_SECONDS or (
                held > STUCK_MOD_SOFT_SECONDS and idle > STUCK_MOD_IDLE_SECONDS
            ):
                return True
        return False

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        if pressed_kc is not None and pressed_kc != combo["trigger"]:
            return False
        if combo["trigger"] not in self._pressed:
            return False
        if not combo["modifiers"]:
            return True
        lingering = self._lingering_modifiers()
        saw_ghost = False
        for mod_set in combo["modifiers"]:
            live = False
            for kc in mod_set:
                held = lingering.get(kc)
                if held is None:
                    continue
                if held > STUCK_MOD_MATCH_SECONDS:
                    saw_ghost = True
                    continue
                live = True
                break
            if not live:
                if saw_ghost:
                    self._reject_ghost_modifiers(lingering)
                return False
        return True

    def _reject_ghost_modifiers(self, lingering: dict[int, float] | None = None) -> None:
        if lingering is None:
            lingering = self._lingering_modifiers()
        ghosts = [kc for kc, held in lingering.items() if held > STUCK_MOD_MATCH_SECONDS]
        if not ghosts:
            return
        print(
            f"[stuck] 热键匹配忽略幽灵修饰键 {ghosts}, 清理 XTest 层",
            flush=True,
        )
        self.ctx.janitor.clear_xtest()
        for kc in ghosts:
            self._pressed.discard(kc)
            self._mod_down_since.pop(kc, None)

    def cleanup(self) -> None:
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        try:
            self.dpy.close()
        except Exception:
            pass
