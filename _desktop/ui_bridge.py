"""跨平台桌面 UI 线程派发桥、Qt 平台选择、框选/浮窗打开与浏览器辅助（供 daemon.py 薄入口复用）。"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

RectTuple = tuple[int, int, int, int]


def configure_qt_platform_env() -> None:
    """Linux Wayland 且 XWayland(`DISPLAY`) 就绪时默认用 Qt xcb，保证浮窗精确定位与零 Mutter 堆叠闪烁。

    用户显式设置了 `QT_QPA_PLATFORM` 时尊重用户值；纯无 XWayland 环境下自然沿用 Wayland。
    """
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def query_virtual_screen_rect(app) -> RectTuple | None:
    """在 UI 主线程查询当前虚拟桌面逻辑矩形 `(x, y, w, h)`。"""
    screen = app.primaryScreen()
    if screen is None:
        return None
    vg = screen.virtualGeometry()
    if vg.width() <= 0 or vg.height() <= 0:
        return None
    return (vg.x(), vg.y(), vg.width(), vg.height())


def open_live_selector(
    app,
    selector_cls,
    rect: RectTuple | None,
    mode_label: str,
    on_selected: Callable[[RectTuple], None],
    on_cancelled: Callable[[], None],
):
    """直接打开透明全屏红十字框选器（不预抓全屏，单屏使用 showFullScreen 杜绝顶栏/Dock 压扁跳动）。"""
    sel = selector_cls(
        virtual_geometry=rect,
        mode_label=mode_label,
        on_selected=on_selected,
        on_cancelled=on_cancelled,
    )
    if len(app.screens()) <= 1:
        sel.showFullScreen()
    else:
        sel.show()
    sel.raise_()
    sel.activateWindow()
    return sel


def open_translate_overlay(overlay_cls, png, rect, *, loading, on_copy_text, on_retry, on_closed):
    """打开原位背景遮挡翻译浮窗并置顶激活。"""
    ov = overlay_cls(
        png,
        rect,
        loading=loading,
        on_copy_text=on_copy_text,
        on_retry=on_retry,
        on_closed=on_closed,
    )
    ov.show()
    ov.raise_()
    ov.activateWindow()
    return ov


class QtUiBridge(QObject):
    """线程安全 UI 派发桥：将后台线程回调投递到 Qt 主线程执行。"""

    _invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, fn: Callable[[], None]) -> None:
        self._invoke.emit(fn)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


def open_url_async(url: str) -> None:
    """后台线程打开默认浏览器，不阻塞 Qt 主循环。"""
    threading.Thread(target=lambda: webbrowser.open(url), name="open-url", daemon=True).start()
