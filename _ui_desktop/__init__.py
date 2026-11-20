"""跨平台桌面原生纯 UI 层（PySide6-Essentials · 方案 C Studio 视觉规范）。

本包严格位于 `ui → biz → logic` 顶层：
- 仅依赖 `_contracts` 数据结构与 `PySide6` 图形控件；
- 通过 Props 与 `Callable` 回调与业务编排层交互；
- 绝不包含 HTTP 请求、持久化读写或平台底层系统命令。
"""
from __future__ import annotations

from _ui_desktop import theme
from _ui_desktop.caption_bar import compute_trailing_two_lines
from _ui_desktop.inplace_overlay import InplaceTranslateOverlay, RenderedPatchInfo
from _ui_desktop.region_selector import RedCrosshairSelector
from _ui_desktop.tray import StudioTrayIcon, render_status_icon, render_status_image
from _ui_desktop.voice_hud import VoiceHudOverlay

__all__ = [
    "InplaceTranslateOverlay",
    "RedCrosshairSelector",
    "RenderedPatchInfo",
    "StudioTrayIcon",
    "VoiceHudOverlay",
    "compute_trailing_two_lines",
    "render_status_icon",
    "render_status_image",
    "theme",
]
