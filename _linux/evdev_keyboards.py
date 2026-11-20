"""本机键盘类 evdev 设备的唯一入口：快捷键监听器与设置页探测共用同一筛选。

键盘类 = 有 EV_KEY、有空格键、A–M 里至少 5 个字母键；带鼠标按键的键盘类设备（RustDesk、ydotoold 虚拟键盘）
同样纳入，只排除本应用自己的注入设备。打开即把设备时钟设为 CLOCK_MONOTONIC，事件时间与合成器时间戳同源。
不依赖 python-evdev 的常量，键码取自 `_hotkey.EVDEV_KEY_CODES`（Linux input-event-codes.h），便于无 evdev 包的隔离环境测试。
"""
from __future__ import annotations

import struct
import time
from collections.abc import Callable, Collection, Iterable
from typing import Any

from _hotkey import EVDEV_KEY_CODES
from _linux.uinput_device import UINPUT_DEVICE_NAME

EV_SYN = 0
EV_KEY = 1
SYN_DROPPED = 3
KEY_ESC = EVDEV_KEY_CODES["KEY_ESC"]
KEY_SPACE = EVDEV_KEY_CODES["KEY_SPACE"]
KEYBOARD_CODE_LIMIT = 0x100  # BTN_* 从 0x100 起，不是键盘键
_LETTERS_A_TO_M = frozenset(EVDEV_KEY_CODES[f"KEY_{c}"] for c in "ABCDEFGHIJKLM")
EVIOCSCLOCKID = 0x400445A0  # _IOW('E', 0xa0, int)


def is_keyboard(capabilities: dict[int, Iterable[Any]]) -> bool:
    keys = set(capabilities.get(EV_KEY) or ())
    return KEY_SPACE in keys and len(keys & _LETTERS_A_TO_M) >= 5


def set_monotonic_clock(fd: int) -> None:
    import fcntl  # 用到时才导入：服务端在 Windows 上也导入本模块（设置页探测的默认实现），那里没有 fcntl

    fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))


def _list_device_paths() -> list[str]:
    from evdev import list_devices

    return list(list_devices())


def _open_device(path: str) -> Any:
    from evdev import InputDevice

    return InputDevice(path)


def open_keyboards(
    exclude_paths: Collection[str] = (),
    *,
    list_paths: Callable[[], list[str]] = _list_device_paths,
    open_device: Callable[[str], Any] = _open_device,
    set_clock: Callable[[int], None] = set_monotonic_clock,
) -> list[Any]:
    """打开全部可读的键盘类设备（跳过 exclude_paths 与本应用注入设备），返回已设 MONOTONIC 时钟的设备。"""
    devices: list[Any] = []
    for path in list_paths():
        if path in exclude_paths:
            continue
        try:
            dev = open_device(path)
        except OSError:
            continue  # 当前用户读不了的节点不是可用键盘
        try:
            usable = dev.name != UINPUT_DEVICE_NAME and is_keyboard(dev.capabilities())
            if usable:
                set_clock(dev.fd)
        except OSError:
            usable = False  # 探测途中断开或拒绝 ioctl：同样不是可用键盘
        if not usable:
            dev.close()
            continue
        devices.append(dev)
    return devices


def readable_keyboard_paths(**kwargs: Any) -> list[str]:
    """设置页探测：当前用户能读取的键盘类设备路径（打开即关，不读事件）。"""
    devices = open_keyboards(**kwargs)
    paths = [dev.path for dev in devices]
    for dev in devices:
        dev.close()
    return paths


__all__ = [
    "EV_KEY",
    "EV_SYN",
    "KEYBOARD_CODE_LIMIT",
    "KEY_ESC",
    "SYN_DROPPED",
    "is_keyboard",
    "open_keyboards",
    "readable_keyboard_paths",
    "set_monotonic_clock",
]
