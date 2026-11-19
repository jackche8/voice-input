"""evdev 内核级全局快捷键，适用于 X11 和 Wayland（观察器/回退 + 物理修饰键探针）。"""
from __future__ import annotations

import threading
import time

from _hotkey import parse_hotkey_evdev
from _linux.hotkey_actions import GLib, HotkeyActionMixin, HotkeyContext
from _linux.uinput_device import UINPUT_DEVICE_NAME

# Linux input-event-codes.h 标准修饰键码（供无 evdev 包环境下的单测也能安全判定）
_LINUX_MOD_KEYCODES = frozenset(
    {
        29,   # KEY_LEFTCTRL
        97,   # KEY_RIGHTCTRL
        42,   # KEY_LEFTSHIFT
        54,   # KEY_RIGHTSHIFT
        56,   # KEY_LEFTALT
        100,  # KEY_RIGHTALT
        125,  # KEY_LEFTMETA
        126,  # KEY_RIGHTMETA
    }
)


class EvdevHotkeyGrabber(HotkeyActionMixin):
    """evdev 内核级全局快捷键，适用于 X11 和 Wayland（支持 voice / screenshot / ocr / repeat 四键）。"""

    def __init__(self, ctx: HotkeyContext, *, devices: list | None = None, combo_parser=parse_hotkey_evdev) -> None:
        self._init_hotkey_actions(ctx)
        self.actions_enabled = True  # portal 接管时作为活动观察器，不触发动作
        self._running = False
        self._thread = None
        self._pressed_keys: set[int] = set()

        self._voice_combo = combo_parser(self.ctx.bindings.voice)
        self._screen_combo = combo_parser(self.ctx.bindings.screenshot)
        self._ocr_combo = combo_parser(self.ctx.bindings.ocr) if self.ctx.bindings.ocr else None
        self._repeat_combo = combo_parser(self.ctx.bindings.repeat)
        print(f"  evdev: 语音快捷键 {self.ctx.bindings.voice} → trigger={self._voice_combo['trigger']} mods={self._voice_combo['modifiers']}")
        print(f"  evdev: 截图快捷键 {self.ctx.bindings.screenshot} → trigger={self._screen_combo['trigger']} mods={self._screen_combo['modifiers']}")
        if self._ocr_combo is not None:
            print(f"  evdev: OCR 快捷键 {self.ctx.bindings.ocr} → trigger={self._ocr_combo['trigger']} mods={self._ocr_combo['modifiers']}")
        print(f"  evdev: 重复快捷键 {self.ctx.bindings.repeat} → trigger={self._repeat_combo['trigger']} mods={self._repeat_combo['modifiers']}")

        if devices is not None:
            self._devices = list(devices)
        else:
            from evdev import InputDevice, list_devices

            self._devices = []
            for path in list_devices():
                try:
                    dev = InputDevice(path)
                    if dev.name == UINPUT_DEVICE_NAME:
                        dev.close()
                        continue
                    if self._is_real_keyboard(dev.capabilities()):
                        self._devices.append(dev)
                        print(f"  evdev: 监控键盘 {dev.name} ({dev.path})")
                    else:
                        dev.close()
                except Exception:
                    pass

        if not self._devices:
            raise RuntimeError(
                "未找到键盘设备。请确保用户属于 input 组: "
                "sudo usermod -aG input $USER 然后重新登录"
            )

    def any_modifier_down(self) -> bool:
        """当前是否有任意物理修饰键处于按下态（供 Wayland 下 ModifierJanitor 查询）。"""
        return bool(self._pressed_keys & _LINUX_MOD_KEYCODES)

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass

    @staticmethod
    def _is_real_keyboard(caps) -> bool:
        """判定一个 evdev 设备是否为真键盘 (排除游戏鼠标宏键、特殊功能键等)."""
        from evdev import ecodes

        if ecodes.EV_KEY not in caps:
            return False
        keys = caps[ecodes.EV_KEY]
        if ecodes.BTN_LEFT in keys:
            return False
        if ecodes.KEY_SPACE not in keys:
            return False
        letter_codes = [getattr(ecodes, f"KEY_{c}") for c in "ABCDEFGHIJKLM"]
        return sum(1 for c in letter_codes if c in keys) >= 5

    def _rescan_devices(self) -> None:
        """重新扫描键盘设备（USB 热插拔恢复）。"""
        from evdev import InputDevice, list_devices

        existing_paths = {d.path for d in self._devices}
        for path in list_devices():
            if path in existing_paths:
                continue
            try:
                dev = InputDevice(path)
                if dev.name == UINPUT_DEVICE_NAME:
                    dev.close()
                    continue
                if self._is_real_keyboard(dev.capabilities()):
                    self._devices.append(dev)
                    print(f"  evdev: 重新发现键盘 {dev.name} ({dev.path})", flush=True)
                else:
                    dev.close()
            except Exception:
                pass

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        import select

        from evdev import ecodes

        print("  evdev: 监控线程已启动", flush=True)
        print(f"  evdev: 监控 {len(self._devices)} 个设备, fds={[d.fd for d in self._devices]}", flush=True)

        rescan_interval = 30.0
        last_rescan = time.time()
        while self._running:
            now = time.time()
            if now - last_rescan >= rescan_interval:
                self._rescan_devices()
                last_rescan = now

            fds = {}
            for dev in list(self._devices):
                try:
                    fds[dev.fd] = dev
                except Exception as e:
                    print(f"  evdev: [ERROR] 获取fd失败 {dev.name}: {e}", flush=True)

            if not fds:
                print("  evdev: [WARN] 无可用设备, 尝试重新扫描...", flush=True)
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(3)
                continue

            try:
                r, _, _ = select.select(list(fds.keys()), [], [], 1.0)
            except Exception as e:
                print(f"  evdev: [ERROR] select 异常: {type(e).__name__}: {e}, 重建设备", flush=True)
                for d in list(self._devices):
                    try:
                        d.close()
                    except Exception:
                        pass
                self._devices.clear()
                self._pressed_keys.clear()
                self._rescan_devices()
                last_rescan = time.time()
                time.sleep(0.5)
                continue

            for fd in r:
                dev = fds.get(fd)
                if not dev:
                    continue
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event.code, event.value)
                except OSError as e:
                    print(f"  evdev: [ERROR] 设备断开 {dev.name}: {e}", flush=True)
                    try:
                        dev.close()
                    except Exception:
                        pass
                    try:
                        self._devices.remove(dev)
                    except ValueError:
                        pass
                    self._pressed_keys.clear()
                except Exception as e:
                    print(f"  evdev: [ERROR] 事件读取异常: {type(e).__name__}: {e}", flush=True)

    def _matches_any_binding(self, code: int) -> bool:
        combos = [self._voice_combo, self._screen_combo, self._repeat_combo]
        if self._ocr_combo is not None:
            combos.append(self._ocr_combo)
        return any(self._match_combo(combo, code) for combo in combos)

    def _handle_key_event(self, code: int, value: int) -> None:
        """处理按键事件。value: 0=释放, 1=按下, 2=自动重复"""
        if value == 1:
            self._pressed_keys.add(code)
            # 修饰键与本应用快捷键组合自身不得误关刚打开的框选器/浮窗；Esc 或其他普通键仍可关闭。
            if code not in _LINUX_MOD_KEYCODES and not self._matches_any_binding(code):
                self._close_overlay_on_key()
            self._check_press(code)
        elif value == 0:
            self._pressed_keys.discard(code)
            if code == self._voice_combo["trigger"] and self.actions_enabled:
                print("  evdev: 语音触发键释放, 调度停止", flush=True)
                GLib.idle_add(self._schedule_release)

    def _match_combo(self, combo: dict, pressed_kc: int | None = None) -> bool:
        """只有刚才按下的键是 trigger 才可能命中，避免陈 trigger 配后续任意键。"""
        if pressed_kc is not None and pressed_kc != combo["trigger"]:
            return False
        if combo["trigger"] not in self._pressed_keys:
            return False
        for mod_keys in combo["modifiers"]:
            if not (mod_keys & self._pressed_keys):
                return False
        return True

    def _check_press(self, pressed_kc: int) -> None:
        if not self.actions_enabled:
            return
        if self._match_combo(self._voice_combo, pressed_kc):
            print("  evdev: ★ 语音快捷键匹配！调度录音", flush=True)
            GLib.idle_add(self._on_voice_press)
        elif self._match_combo(self._screen_combo, pressed_kc):
            print("  evdev: ★ 截图快捷键匹配！调度截图", flush=True)
            GLib.idle_add(self._on_screenshot)
        elif self._ocr_combo is not None and self._match_combo(self._ocr_combo, pressed_kc):
            print("  evdev: ★ OCR 快捷键匹配！调度纯 OCR", flush=True)
            GLib.idle_add(self._on_ocr)
        elif self._match_combo(self._repeat_combo, pressed_kc):
            print("  evdev: ★ 重复快捷键匹配！调度重复", flush=True)
            GLib.idle_add(self._on_repeat)

    def cleanup(self) -> None:
        self._running = False
        if self._pending_release_id:
            GLib.source_remove(self._pending_release_id)
            self._pending_release_id = 0
        if self._thread:
            self._thread.join(timeout=2)
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
