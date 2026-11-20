"""xdg-desktop-portal 的 D-Bus 公共部分：主循环、接口版本、host app 注册。

Wayland 下由 compositor 独占按键 / 提供整屏截图。dbus-python 必须接到 GLib 主循环，
portal 信号才会在 GTK 主循环里派发。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"

# 本应用向 portal host Registry 登记的 app id（GNOME 全局快捷键授权表里以它为键）。唯一来源。
DAEMON_APP_ID = "ollama-voice-input-daemon"
# 旧版本在没有上面条目时曾退回项目根 .desktop 用这个 id 登记过；只用于把这些旧登记认作自己的（冲突检查）
LEGACY_APP_ID = "ollama-voice-input"
OWN_APP_IDS = (DAEMON_APP_ID, LEGACY_APP_ID)
PORTAL_APP_ENTRY = f"{DAEMON_APP_ID}.desktop"


def portal_app_entry_candidates(environ: Mapping[str, str]) -> list[Path]:
    """XDG 规范的应用条目查找顺序：$XDG_DATA_HOME（缺省 ~/.local/share）在前，再 $XDG_DATA_DIRS（缺省 /usr/local/share:/usr/share）。"""
    home = environ.get("XDG_DATA_HOME", "")
    data_home = Path(home) if home and Path(home).is_absolute() else Path(environ.get("HOME", str(Path.home()))) / ".local/share"
    dirs = [Path(d) for d in (environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d and Path(d).is_absolute()]
    return [base / "applications" / PORTAL_APP_ENTRY for base in (data_home, *dirs)]


def find_portal_app_entry(environ: Mapping[str, str]) -> Path | None:
    return next((path for path in portal_app_entry_candidates(environ) if path.is_file()), None)

_dbus_loop_ready = False


def ensure_dbus_glib_loop() -> bool:
    """把 dbus-python 的默认主循环接到 GLib，保证 portal 信号在 GTK 主循环里派发。"""
    global _dbus_loop_ready
    if _dbus_loop_ready:
        return True
    try:
        import dbus.mainloop.glib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        _dbus_loop_ready = True
        return True
    except Exception:
        return False


def dbus_module():
    if not ensure_dbus_glib_loop():
        raise RuntimeError("dbus-python / GLib 主循环不可用")
    import dbus

    return dbus


def portal_interface_version(interface: str) -> int | None:
    """返回 portal 接口版本；不存在/不可达返回 None。"""
    try:
        dbus = dbus_module()
        bus = dbus.SessionBus()
        obj = bus.get_object(PORTAL_NAME, PORTAL_PATH)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return int(props.Get(interface, "version"))
    except Exception:
        return None


def portal_available(interface: str) -> bool:
    return portal_interface_version(interface) is not None



HOST_REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"
_PORTAL_HOST_REGISTERED = False


def register_portal_host_app(bus=None, *, force: bool = False, dbus_mod=None) -> bool:
    """把本进程注册成 host app，给 xdg-desktop-portal 一个明确的 app_id。

    systemd 服务没有桌面上下文，门户会报 "An app id is required"；
    org.freedesktop.host.portal.Registry 是官方给非沙箱宿主应用的注册入口。
    `force=True` 时绕过进程级缓存强制重注册（用于 portal 进程重启后恢复）。
    """
    global _PORTAL_HOST_REGISTERED
    if _PORTAL_HOST_REGISTERED and not force:
        return True
    try:
        dbus = dbus_mod if dbus_mod is not None else dbus_module()
        if bus is None:
            bus = dbus.SessionBus()
        obj = bus.get_object(PORTAL_NAME, PORTAL_PATH)
        iface = dbus.Interface(obj, HOST_REGISTRY_IFACE)
        desktop = find_portal_app_entry(os.environ)
        if desktop is None:  # 应用启动时 ensure_portal_app_entry 会写好；仍没有就如实失败，不退回别的 app id
            raise FileNotFoundError(f"XDG 数据目录里没有 {PORTAL_APP_ENTRY}（{portal_app_entry_candidates(os.environ)[0].parent}）")
        app_id = DAEMON_APP_ID
        options = dbus.Dictionary(
            {"desktop_entry": dbus.String(str(desktop))}, signature="sv"
        )
        iface.Register(app_id, options)
        _PORTAL_HOST_REGISTERED = True
        suffix = "（重启后重注册）" if force else ""
        print(f"  快捷键: portal host app 已注册{suffix} ({app_id})", flush=True)
        return True
    except Exception as exc:
        print(f"  快捷键: portal host app 注册失败 ({exc})", flush=True)
        return False
