"""桌面集成（应用主进程，业务层）：开机自启条目与 Linux 门户 app id 条目的落地。

- 自启：命令一律是 `_paths.app_command(paths, background=True)`（后台启动，不弹主窗口）；设置页开关经 config_changed 即时生效。
- 门户条目：GNOME 全局快捷键授权按 app id 记，xdg-desktop-portal 要能在 XDG 数据目录找到 `<app id>.desktop`；
  所有候选目录都没有时写到用户目录（已有的不覆盖：可能是安装包放的，或用户自己改过）。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from _paths import APP_DISPLAY_NAME, AppPaths, app_command
from _platform.protocol import PlatformAdapter


def apply_autostart(adapter: PlatformAdapter, paths: AppPaths, enabled: bool) -> None:
    adapter.configure_autostart(enabled=enabled, command=app_command(paths, background=True))


def ensure_portal_app_entry(paths: AppPaths, environ: Mapping[str, str]) -> Path:
    """返回门户实际会用到的条目路径；缺失时写入 $XDG_DATA_HOME/applications（缺省 ~/.local/share）。"""
    from _linux.portal_dbus import find_portal_app_entry, portal_app_entry_candidates
    from _platform.linux import LinuxPlatformAdapter

    found = find_portal_app_entry(environ)
    if found is not None:
        return found
    target = portal_app_entry_candidates(environ)[0]
    entry = LinuxPlatformAdapter.render_autostart_desktop(app_command(paths, background=True), name=APP_DISPLAY_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry + "NoDisplay=true\n", encoding="utf-8")  # 只作门户身份，不出现在应用菜单
    return target


__all__ = ["apply_autostart", "ensure_portal_app_entry"]
