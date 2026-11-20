"""evdev 读线程：按设备记录物理按下集合，把组合的按下区间（开始 s / 结束 e，单调毫秒）上报给 sink。

一次按下 = 组合由不满足变满足（s）到由满足变不满足（e，任一键松开、设备断开都算）。门户模式只报区间，
开始由 GNOME `Activated` 放行；回退模式（门户不可用）在 trigger 最后按下时另报放行。组合定义由 manager 经
`set_combos` 下发（门户模式 = GNOME 实际绑定，回退模式 = 配置）。规则见 8.6-F。

读线程每轮 select 先读完全部就绪设备、按事件时间合并后处理（跨设备节点的组合按真实先后判定），
本轮结束投递一次读线程进度；线程意外退出时经 sink.source_failed 上报，不静默消失。
"""
from __future__ import annotations

import select
import threading
import time
import traceback
from collections.abc import Callable, Collection, Mapping
from typing import Any

from _hotkey import EVDEV_MODIFIER_CODES
from _linux.evdev_keyboards import (
    EV_KEY,
    EV_SYN,
    KEY_ESC,
    KEYBOARD_CODE_LIMIT,
    SYN_DROPPED,
    open_keyboards,
)
from _linux.hotkey_actions import GLib, HotkeyContext, HotkeySink, end_interval_and_release
from _linux.hotkey_state import Action

_LINUX_MOD_KEYCODES = frozenset().union(*EVDEV_MODIFIER_CODES.values())
_MASK = 0xFFFFFFFF
RESCAN_INTERVAL_S = 30.0
# python-evdev 2.0.0 `InputDevice.read()` 单次最多读 64 个事件（input.c:95 `struct input_event event[64]`），
# 读满说明设备缓冲里可能还有更早入队、本轮没读到的事件
EVDEV_READ_MAX = 64

Combo = Mapping[str, Any]  # parse_hotkey_evdev 的结果：{"trigger": int, "modifiers": [set[int], ...]}


def _now_ms() -> int:
    return int(time.monotonic() * 1000) & _MASK


def _event_ms(event: Any) -> int:
    return (int(event.sec) * 1000 + int(event.usec) // 1000) & _MASK


def _satisfied(combo: Combo, keys: set[int]) -> bool:
    return combo["trigger"] in keys and all(mods & keys for mods in combo["modifiers"])


class EvdevHotkeyGrabber:
    """全部本机键盘类设备的读线程：上报按下区间、回退模式下的放行与 Esc。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        sink: HotkeySink,
        *,
        open_devices: Callable[[Collection[str]], list[Any]] = open_keyboards,
    ) -> None:
        self.ctx = ctx
        self.sink = sink
        self.activations_enabled = False  # 回退模式（门户不可用）由 manager 打开
        self._open_devices = open_devices
        self._lock = threading.Lock()
        self._devices: dict[str, Any] = {}
        self._pressed: dict[str, set[int]] = {}
        self._combos: dict[Action, Combo] = {}
        self._held: set[Action] = set()
        self._running = False
        self._thread: threading.Thread | None = None

        devices = open_devices(())
        if not devices:
            raise RuntimeError(
                "未找到可读的键盘设备。请确保用户属于 input 组: sudo usermod -aG input $USER 然后重新登录"
            )
        with self._lock:
            added = [dev for dev in devices if self._add_device_locked(dev)]
        if not added:
            raise RuntimeError("键盘设备打开后读取当前按键失败（设备已断开）")

    # ---- manager 调用 ----------------------------------------------------
    def set_combos(self, combos: Mapping[Action, Combo]) -> None:
        """替换组合定义并按当前按下集合重算：替换引起的按住变化照常上报区间起止。"""
        t_ms = _now_ms()
        with self._lock:
            for action in sorted(self._held - set(combos)):
                self._held.discard(action)
                self._post_end_locked(action, t_ms)
            self._combos = dict(combos)
            self._recompute_locked(t_ms)
        for action, combo in combos.items():
            print(f"  evdev: {action} → trigger={combo['trigger']} mods={combo['modifiers']}", flush=True)

    def any_modifier_down(self) -> bool:
        """当前是否有任意物理修饰键处于按下态（供 Wayland 下 ModifierJanitor 查询）。"""
        with self._lock:
            return bool(self._union_locked() & _LINUX_MOD_KEYCODES)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._monitor, name="evdev-hotkeys", daemon=True)
        self._thread.start()

    def cleanup(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            for dev in self._devices.values():
                dev.close()
            self._devices.clear()
            self._pressed.clear()

    # ---- 读线程 ----------------------------------------------------------
    def _monitor(self) -> None:
        print(f"  evdev: 监控 {len(self._devices)} 个设备 {sorted(self._devices)}", flush=True)
        try:
            self._run()
        except Exception as exc:
            # 意外异常：读线程结束，经失败通道上报（manager 转到底栏与日志），不静默退出
            traceback.print_exc()
            self._running = False
            GLib.idle_add(self.sink.source_failed, f"evdev 读线程意外退出：{type(exc).__name__}: {exc}")

    def _run(self) -> None:
        last_rescan = time.monotonic()
        while self._running:
            if time.monotonic() - last_rescan >= RESCAN_INTERVAL_S or not self._devices:
                self._rescan()
                last_rescan = time.monotonic()
            if not self._devices:
                time.sleep(3)  # 没有设备时等热插拔，下一轮重扫
                continue
            self._poll_once()

    def _poll_once(self) -> None:
        """一轮：select → 读完全部就绪设备 → 按事件时间合并处理 → 投递一次进度。

        进度取本轮 select 之前的单调时间：内核在入队时打时间戳，凡时间戳早于它的事件在 select 前已在
        设备缓冲里，select 会报就绪、本轮读到；所以进度不会越过任何尚未读到的事件。例外是某设备一次读满
        （EVDEV_READ_MAX）：剩余事件留到下一轮，本轮不投递进度。
        """
        progress_ms = _now_ms()
        with self._lock:
            by_fd = {dev.fd: path for path, dev in self._devices.items()}
        try:
            ready, _, _ = select.select(list(by_fd), [], [], 1.0)
        except (OSError, ValueError) as exc:
            print(f"  evdev: [ERROR] select 异常 {type(exc).__name__}: {exc}，重建设备", flush=True)
            self._drop_all()
            return
        batch: list[tuple[str, Any]] = []
        lost: list[str] = []
        read_full = False
        for fd in ready:
            path = by_fd[fd]
            with self._lock:
                dev = self._devices.get(path)
            if dev is None:
                continue
            try:
                events = list(dev.read())
            except OSError as exc:
                print(f"  evdev: 设备断开 {dev.name} ({path}): {exc}", flush=True)
                lost.append(path)
                continue
            read_full = read_full or len(events) >= EVDEV_READ_MAX
            batch.extend((path, event) for event in events)
        batch.sort(key=lambda item: (item[1].sec, item[1].usec))  # 稳定排序：同一时刻保持各设备内顺序
        for path, event in batch:
            self._handle_event(path, event)
        for path in lost:
            self._drop_device(path, _now_ms())
        if read_full:
            return
        with self._lock:
            # 同一队列、排在本轮全部区间投递之后：主线程处理到它时，早于进度的区间都已记入状态
            GLib.idle_add(self.sink.reader_progress, progress_ms)

    def _handle_event(self, path: str, event: Any) -> None:
        if event.type == EV_SYN and event.code == SYN_DROPPED:
            self._resync(path, _event_ms(event))
            return
        if event.type != EV_KEY or event.code >= KEYBOARD_CODE_LIMIT or event.value == 2:
            return
        t_ms = _event_ms(event)
        with self._lock:
            keys = self._pressed.get(path)
            if keys is None:
                return  # 本轮前面已按断开移除的设备：同批里它剩下的事件不再计入
            if event.value == 1:
                keys.add(event.code)
            else:
                keys.discard(event.code)
            self._recompute_locked(t_ms)
            if event.value == 1 and self.activations_enabled:
                for action, combo in self._combos.items():
                    if combo["trigger"] == event.code and action in self._held:
                        GLib.idle_add(self.sink.activated, action, t_ms)
        if event.value == 1 and event.code == KEY_ESC:
            self.sink.escape_pressed()

    def _resync(self, path: str, t_ms: int) -> None:
        """SYN_DROPPED：缓冲溢出丢了事件，按设备当前按键重同步；此时设备已断开则按断开处理。"""
        with self._lock:
            dev = self._devices.get(path)
            if dev is None:
                return
            try:
                self._pressed[path] = {c for c in dev.active_keys() if c < KEYBOARD_CODE_LIMIT}
            except OSError as exc:
                failure: OSError | None = exc
            else:
                failure = None
                self._recompute_locked(t_ms)
        if failure is not None:
            print(f"  evdev: 设备断开 {dev.name} ({path}): {failure}", flush=True)
            self._drop_device(path, t_ms)

    def _rescan(self) -> None:
        with self._lock:
            known = set(self._devices)
        new_devices = self._open_devices(known)
        if not new_devices:
            return
        with self._lock:
            for dev in new_devices:
                if self._add_device_locked(dev):
                    print(f"  evdev: 发现键盘 {dev.name} ({dev.path})", flush=True)
            self._recompute_locked(_now_ms())

    def _drop_device(self, path: str, t_ms: int) -> None:
        with self._lock:
            dev = self._devices.pop(path, None)
            self._pressed.pop(path, None)
            self._recompute_locked(t_ms)
        if dev is not None:
            dev.close()

    def _drop_all(self) -> None:
        with self._lock:
            devices = list(self._devices.values())
            self._devices.clear()
            self._pressed.clear()
            self._recompute_locked(_now_ms())
        for dev in devices:
            dev.close()

    # ---- 锁内辅助 --------------------------------------------------------
    def _add_device_locked(self, dev: Any) -> bool:
        """按设备当前按键入列；读当前按键时已断开则关闭跳过（与打开探测途中断开同样处理）。"""
        try:
            keys = {c for c in dev.active_keys() if c < KEYBOARD_CODE_LIMIT}
        except OSError as exc:
            print(f"  evdev: 跳过已断开的设备 {dev.name} ({dev.path}): {exc}", flush=True)
            dev.close()
            return False
        self._devices[dev.path] = dev
        self._pressed[dev.path] = keys
        return True

    def _union_locked(self) -> set[int]:
        union: set[int] = set()
        for keys in self._pressed.values():
            union |= keys
        return union

    def _recompute_locked(self, t_ms: int) -> None:
        """按并集重算每个组合是否按住；变化即投递区间起止（锁内投递，保证与其它线程的投递顺序一致）。"""
        union = self._union_locked()
        for action, combo in self._combos.items():
            held = _satisfied(combo, union)
            if held and action not in self._held:
                self._held.add(action)
                GLib.idle_add(self.sink.interval_started, action, t_ms)
            elif not held and action in self._held:
                self._held.discard(action)
                self._post_end_locked(action, t_ms)

    def _post_end_locked(self, action: Action, t_ms: int) -> None:
        # 回退模式由本后端判定开始，停录后补发修饰键抬起（现状语义）；门户模式只报结束
        if self.activations_enabled:
            GLib.idle_add(end_interval_and_release, self.sink, self.ctx, action, t_ms)
        else:
            GLib.idle_add(self.sink.interval_ended, action, t_ms)
