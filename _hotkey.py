"""快捷键字符串的纯逻辑：规范化、显示、Portal 触发名、GNOME accelerator、evdev 键码。

不抓取真实键鼠，不导入 Xlib/GTK。默认键位 Ctrl+Alt+Z/X/R 的唯一来源是 `_contracts.AppConfig`。
抓取后端在 `_linux/hotkey_*.py`，都从这里取解析结果，不各写一份。
"""
from __future__ import annotations

from dataclasses import dataclass

MOD_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "primary": "ctrl",
    "option": "alt",
    "mod1": "alt",
    "mod4": "super",
    "win": "super",
    "cmd": "super",
}


@dataclass(frozen=True)
class HotkeyBindings:
    """一次启动内四条快捷键的值，来自配置，不在平台层写死。"""

    voice: str
    screenshot: str
    repeat: str
    ocr: str = "ctrl+alt+o"


@dataclass(frozen=True)
class KeyName:
    """一个规范主键在跨平台抓取后端里的名字（唯一键名表，后端都从这里取，不各写一份）。

    keysym：XKB/X11 keysym 名（python-xlib `XK.string_to_keysym` 与 GNOME accelerator 同名）；
    portal：GlobalShortcuts `preferred_trigger` 的主键写法——xdg-desktop-portal-gnome 50.0
    `portal_trigger_to_settings` 只换修饰键、主键原样拼进 GTK accelerator，所以必须是 keysym 名
    （"`" 在 Gdk/xkbcommon 中都解析不到，故 grave 写 "grave"）；
    evdev：`evdev.ecodes` 属性名；
    vk：Windows Win32 Virtual-Key 码；
    macos_keycode：macOS Quartz/Carbon `kVK_*` 虚拟键码。
    """

    keysym: str
    portal: str
    evdev: str
    vk: int = 0
    macos_keycode: int = 0


_MACOS_KEYCODES: dict[str, int] = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "equal": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "minus": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "o": 0x1F,
    "u": 0x20,
    "i": 0x22,
    "p": 0x23,
    "enter": 0x24,
    "l": 0x25,
    "j": 0x26,
    "k": 0x28,
    "semicolon": 0x29,
    "backslash": 0x2A,
    "comma": 0x2B,
    "slash": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    "dot": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "grave": 0x32,
    "backspace": 0x33,
    "escape": 0x35,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f3": 0x63,
    "f8": 0x64,
    "f9": 0x65,
    "f11": 0x67,
    "f10": 0x6D,
    "f12": 0x6F,
    "delete": 0x75,
    "f4": 0x76,
    "f2": 0x78,
    "f1": 0x7A,
}


def _build_key_table() -> dict[str, KeyName]:
    table: dict[str, KeyName] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        table[c] = KeyName(
            keysym=c,
            portal=c.upper(),
            evdev=f"KEY_{c.upper()}",
            vk=ord(c.upper()),
            macos_keycode=_MACOS_KEYCODES[c],
        )
    for d in "0123456789":
        table[d] = KeyName(
            keysym=d,
            portal=d,
            evdev=f"KEY_{d}",
            vk=ord(d),
            macos_keycode=_MACOS_KEYCODES[d],
        )
    for i in range(1, 13):
        key = f"f{i}"
        table[key] = KeyName(
            keysym=f"F{i}",
            portal=f"F{i}",
            evdev=f"KEY_F{i}",
            vk=0x70 + (i - 1),
            macos_keycode=_MACOS_KEYCODES[key],
        )
    for name, keysym, portal, evdev, vk in (
        ("grave", "grave", "grave", "KEY_GRAVE", 0xC0),
        ("space", "space", "space", "KEY_SPACE", 0x20),
        ("tab", "Tab", "Tab", "KEY_TAB", 0x09),
        ("enter", "Return", "Return", "KEY_ENTER", 0x0D),
        ("escape", "Escape", "Escape", "KEY_ESC", 0x1B),
        ("backspace", "BackSpace", "BackSpace", "KEY_BACKSPACE", 0x08),
        ("delete", "Delete", "Delete", "KEY_DELETE", 0x2E),
        ("minus", "minus", "minus", "KEY_MINUS", 0xBD),
        ("equal", "equal", "equal", "KEY_EQUAL", 0xBB),
        ("semicolon", "semicolon", "semicolon", "KEY_SEMICOLON", 0xBA),
        ("comma", "comma", "comma", "KEY_COMMA", 0xBC),
        ("dot", "period", "period", "KEY_DOT", 0xBE),
        ("slash", "slash", "slash", "KEY_SLASH", 0xBF),
        ("backslash", "backslash", "backslash", "KEY_BACKSLASH", 0xDC),
    ):
        table[name] = KeyName(
            keysym=keysym,
            portal=portal,
            evdev=evdev,
            vk=vk,
            macos_keycode=_MACOS_KEYCODES[name],
        )
    return table


# 规范主键名 → 各后端名字。含旧配置可能用到的键（escape/minus 等），抓取后端照常解析；
# 设置写入允许的子集见 HOTKEY_MAIN_KEYS。
KEY_TABLE = _build_key_table()
KEY_ALIASES = {"`": "grave", "return": "enter", "esc": "escape", "period": "dot"}
for _name, _key in KEY_TABLE.items():  # keysym 名（小写）→ 规范名，如 backspace、f1、return
    KEY_ALIASES.setdefault(_key.keysym.lower(), _name)


def canonical_key(name: str) -> str:
    """主键名规范化（大小写、`/return/esc/period 等别名）。不认识的名字原样小写返回。"""
    key = name.strip().lower()
    return key if key in KEY_TABLE else KEY_ALIASES.get(key, key)


def normalize_hotkey(hotkey: str) -> str:
    parts = [item.strip().lower() for item in hotkey.replace("-", "+").split("+") if item.strip()]
    mapped = [MOD_ALIASES.get(item, item) for item in parts]
    if mapped:
        mapped[-1] = canonical_key(mapped[-1])
    return "+".join(mapped)


# 设置写入的唯一规则（批准计划 L1）：主键 = a-z、0-9、grave、space、tab、enter、F1–F12，
# 三套后端名字都取自 KEY_TABLE。
HOTKEY_MODIFIERS = ("ctrl", "alt", "shift", "super")
HOTKEY_MAIN_KEYS = frozenset(
    [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(i) for i in range(10)]
    + ["grave", "space", "tab", "enter"]
    + [f"f{i}" for i in range(1, 13)]
)
assert HOTKEY_MAIN_KEYS <= KEY_TABLE.keys()


def validate_hotkey(hotkey: str) -> str:
    """规范化并校验一条快捷键，返回规范串（修饰键按 ctrl/alt/shift/super 顺序 + 主键）。

    规则：至少一个修饰键（ctrl/alt/shift/super 任一，含仅 shift——会占用该键的普通输入，属用户可选，
    提醒由界面给出）；修饰键不重复；恰好一个主键且在 HOTKEY_MAIN_KEYS 内。
    不合法抛 LogicError(invalid_request)，message 写明收到什么，details={"kind": …, "value": 原值}，
    kind ∈ empty | modifier_only | unknown_modifier | duplicate_modifier | no_modifier | unsupported_key。
    """
    from _contracts import ErrorCode, LogicError

    kind = ""

    def bad(why: str) -> LogicError:
        return LogicError(ErrorCode.invalid_request, f"快捷键 {hotkey!r} 不可用：{why}", {"kind": kind, "value": str(hotkey)})

    parts = normalize_hotkey(str(hotkey)).split("+") if str(hotkey).strip() else []
    if not parts or parts == [""]:
        kind = "empty"
        raise bad("为空")
    *mods, main = parts
    if main in HOTKEY_MODIFIERS:
        kind = "modifier_only"
        raise bad("只有修饰键，缺少主键")
    unknown = [m for m in mods if m not in HOTKEY_MODIFIERS]
    if unknown:
        kind = "unknown_modifier"
        raise bad(f"{'+'.join(unknown)} 不是修饰键（只支持 ctrl/alt/shift/super），或包含多个主键")
    if len(set(mods)) != len(mods):
        kind = "duplicate_modifier"
        raise bad("修饰键重复")
    if not mods:
        kind = "no_modifier"
        raise bad("至少需要一个修饰键（Ctrl/Alt/Shift/Super）")
    if main not in HOTKEY_MAIN_KEYS:
        kind = "unsupported_key"
        raise bad(f"不支持主键 {main!r}（支持 a-z、0-9、grave、space、tab、enter、f1-f12）")
    ordered = [m for m in HOTKEY_MODIFIERS if m in mods]
    return "+".join(ordered + [main])


def ensure_distinct_hotkeys(hotkeys: dict[str, str]) -> None:
    """三条快捷键规范化后不得重复；值须已经过 validate_hotkey。重复抛 LogicError(invalid_request)。"""
    from _contracts import ErrorCode, LogicError

    by_value: dict[str, list[str]] = {}
    for name, value in hotkeys.items():
        by_value.setdefault(value, []).append(name)
    dup = {value: names for value, names in by_value.items() if len(names) > 1}
    if dup:
        desc = "；".join(f"{'、'.join(names)} 都是 {value}" for value, names in dup.items())
        listing = "，".join(f"{name}={value}" for name, value in hotkeys.items())
        raise LogicError(ErrorCode.invalid_request, f"快捷键重复：{desc}（当前 {listing}）")


def format_hotkey(hotkey_str: str) -> str:
    """将配置字符串格式化为用户友好的显示文本（Ctrl+Alt+Z）。"""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        return hotkey_str
    mod_map = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super"}
    mods = [mod_map.get(p, p.capitalize()) for p in parts[:-1]]
    key = parts[-1]
    if key == "grave":
        key = "`"
    elif len(key) == 1:
        key = key.upper()
    else:
        key = key.capitalize()
    return "+".join(mods + [key])


# xdg-desktop-portal-gnome 50.0 src/globalshortcuts.c portal_trigger_to_settings 只认
# CTRL/SHIFT/ALT/NUM/LOGO（XDG shortcuts 规范），没有 SUPER。
PORTAL_MODIFIERS = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}


def to_portal_trigger(hotkey: str) -> str:
    """ctrl+alt+z → CTRL+ALT+Z；super → LOGO；主键取 KEY_TABLE 的 portal 名（keysym 名）。"""
    parts = [p.strip() for p in hotkey.lower().split("+") if p.strip()]
    if not parts:
        return hotkey
    mods = [PORTAL_MODIFIERS.get(p, p.upper()) for p in parts[:-1]]
    key = canonical_key(parts[-1])
    name = KEY_TABLE.get(key)
    return "+".join(mods + [name.portal if name else (key.upper() if len(key) == 1 else key)])


def normalize_accel(accel: str) -> str:
    """把 GNOME 的 <Primary><Alt>d 形式归一化成 ctrl+alt+d。"""
    s = accel.strip().lower()
    for token, repl in (
        ("<primary>", "ctrl+"),
        ("<control>", "ctrl+"),
        ("<alt>", "alt+"),
        ("<shift>", "shift+"),
        ("<super>", "super+"),
        ("<mod1>", "alt+"),
        ("<mod4>", "super+"),
    ):
        s = s.replace(token, repl)
    s = s.replace("<", "").replace(">", "")
    parts = [p for p in s.split("+") if p]
    if not parts:
        return ""
    parts[-1] = canonical_key(parts[-1])
    return "+".join(parts)


# Linux input-event-codes.h 中 KEY_TABLE 与修饰键用到的键码（内核 ABI，固定不变）。直接取数，组合解析不依赖
# python-evdev，闸门的隔离环境（无 python-evdev）也能跑真实 evdev 后端回放；数值与 evdev.ecodes 同名常量一致，
# 主键部分由 tests/platform/test_capture_hotkey.py 在装了 python-evdev 的解释器里复核。
EVDEV_KEY_CODES: dict[str, int] = {
    "KEY_ESC": 1, "KEY_1": 2, "KEY_2": 3, "KEY_3": 4, "KEY_4": 5, "KEY_5": 6, "KEY_6": 7, "KEY_7": 8,
    "KEY_8": 9, "KEY_9": 10, "KEY_0": 11, "KEY_MINUS": 12, "KEY_EQUAL": 13, "KEY_BACKSPACE": 14, "KEY_TAB": 15,
    "KEY_Q": 16, "KEY_W": 17, "KEY_E": 18, "KEY_R": 19, "KEY_T": 20, "KEY_Y": 21, "KEY_U": 22, "KEY_I": 23,
    "KEY_O": 24, "KEY_P": 25, "KEY_ENTER": 28, "KEY_A": 30, "KEY_S": 31, "KEY_D": 32, "KEY_F": 33, "KEY_G": 34,
    "KEY_H": 35, "KEY_J": 36, "KEY_K": 37, "KEY_L": 38, "KEY_SEMICOLON": 39, "KEY_GRAVE": 41, "KEY_BACKSLASH": 43,
    "KEY_Z": 44, "KEY_X": 45, "KEY_C": 46, "KEY_V": 47, "KEY_B": 48, "KEY_N": 49, "KEY_M": 50, "KEY_COMMA": 51,
    "KEY_DOT": 52, "KEY_SLASH": 53, "KEY_SPACE": 57, "KEY_F1": 59, "KEY_F2": 60, "KEY_F3": 61, "KEY_F4": 62,
    "KEY_F5": 63, "KEY_F6": 64, "KEY_F7": 65, "KEY_F8": 66, "KEY_F9": 67, "KEY_F10": 68, "KEY_F11": 87,
    "KEY_F12": 88, "KEY_DELETE": 111,
}
EVDEV_MODIFIER_CODES: dict[str, frozenset[int]] = {
    "ctrl": frozenset({29, 97}),  # KEY_LEFTCTRL / KEY_RIGHTCTRL
    "alt": frozenset({56, 100}),  # KEY_LEFTALT / KEY_RIGHTALT
    "shift": frozenset({42, 54}),  # KEY_LEFTSHIFT / KEY_RIGHTSHIFT
    "super": frozenset({125, 126}),  # KEY_LEFTMETA / KEY_RIGHTMETA
}


def parse_hotkey_evdev(hotkey_str: str) -> dict:
    """解析快捷键字符串为 evdev 格式 (内核级，Wayland 兼容)。

    返回: {'trigger': evdev_keycode, 'modifiers': [set_of_keycodes, ...]}
    严格解析：不认识的修饰键或主键直接报错（GNOME 实际绑定可能带 Hyper/Meta 等，少算一个修饰键会误判按住）。
    """
    parts = hotkey_str.lower().strip().split("+")
    *mods, trigger_name = parts
    unknown = [mod for mod in mods if mod not in EVDEV_MODIFIER_CODES]
    if unknown:
        raise ValueError(f"无法解析为 evdev 组合：不认识的修饰键 {unknown}（{hotkey_str!r}）")
    name = KEY_TABLE.get(canonical_key(trigger_name))
    if name is None:
        raise ValueError(f"无法解析为 evdev 组合：不认识的主键 {trigger_name!r}（{hotkey_str!r}）")
    return {"trigger": EVDEV_KEY_CODES[name.evdev], "modifiers": [set(EVDEV_MODIFIER_CODES[mod]) for mod in mods]}


WIN32_MODIFIERS = {
    "alt": 0x0001,      # MOD_ALT
    "ctrl": 0x0002,     # MOD_CONTROL
    "shift": 0x0004,    # MOD_SHIFT
    "super": 0x0008,    # MOD_WIN
}
WIN32_MOD_NOREPEAT = 0x4000

MACOS_MODIFIERS = {
    "shift": 0x00020000,   # kCGEventFlagMaskShift
    "ctrl": 0x00040000,    # kCGEventFlagMaskControl
    "alt": 0x00080000,     # kCGEventFlagMaskAlternate
    "super": 0x00100000,   # kCGEventFlagMaskCommand
}


def parse_hotkey_win32(hotkey_str: str) -> tuple[int, int]:
    """解析规范快捷键为 Win32 RegisterHotKey 所需的 (fsModifiers, vk)。"""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    mods = 0
    for mod in parts[:-1]:
        flag = WIN32_MODIFIERS.get(MOD_ALIASES.get(mod, mod))
        if flag is None:
            raise ValueError(f"Unknown Win32 modifier: {mod}")
        mods |= flag
    name = KEY_TABLE.get(canonical_key(parts[-1]))
    if name is None or not name.vk:
        raise ValueError(f"Unknown Win32 key: {parts[-1]}")
    return mods, name.vk


def parse_hotkey_macos(hotkey_str: str) -> tuple[int, int]:
    """解析规范快捷键为 macOS CGEvent 所需的 (CGEventFlags 掩码, CGKeyCode)。"""
    parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    flags = 0
    for mod in parts[:-1]:
        flag = MACOS_MODIFIERS.get(MOD_ALIASES.get(mod, mod))
        if flag is None:
            raise ValueError(f"Unknown macOS modifier: {mod}")
        flags |= flag
    key = canonical_key(parts[-1])
    name = KEY_TABLE.get(key)
    if name is None:
        raise ValueError(f"Unknown macOS key: {parts[-1]}")
    return flags, name.macos_keycode
