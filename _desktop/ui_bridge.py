"""跨平台桌面 UI 线程派发桥、Qt 平台选择、框选/浮窗打开（供 daemon.py 薄入口复用）。"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot

RectTuple = tuple[int, int, int, int]
AnnotateStyle = tuple[str, str]


def configure_qt_platform_env() -> None:
    """Linux Wayland 且 XWayland(`DISPLAY`) 就绪时默认用 Qt xcb，保证浮窗精确定位与零 Mutter 堆叠闪烁。

    用户显式设置了 `QT_QPA_PLATFORM` 时尊重用户值；纯无 XWayland 环境下自然沿用 Wayland。
    """
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def apply_qt_app_icon(app, paths, *, icon_factory=None) -> bool:
    """为 Qt 主进程设置应用级窗口图标：优先加载 `.ico`（走 QtGui 内置 qico 插件，无需 QtSvg），缺失时回退 `.svg`。"""
    factory = icon_factory
    if factory is None:
        from PySide6.QtGui import QIcon

        factory = QIcon
    res_dir = Path(paths.program_root) / "resources"
    for filename in ("ollama-voice-input.ico", "ollama-voice-input.svg"):
        candidate = res_dir / filename
        if not candidate.is_file():
            continue
        icon = factory(str(candidate))
        if not icon.isNull():
            app.setWindowIcon(icon)
            return True
    return False


def query_virtual_screen_rect(app) -> RectTuple | None:
    """在 UI 主线程查询当前虚拟桌面逻辑矩形 `(x, y, w, h)`。"""
    screen = app.primaryScreen()
    if screen is None:
        return None
    vg = screen.virtualGeometry()
    if vg.width() <= 0 or vg.height() <= 0:
        return None
    return (vg.x(), vg.y(), vg.width(), vg.height())


def open_frozen_selector(
    app,
    selector_cls,
    frozen_png: bytes,
    rect: RectTuple | None,
    mode_label: str,
    on_selected: Callable[[RectTuple], None],
    on_cancelled: Callable[[], None],
):
    """打开冻结全屏红十字框选器（展示已抓拍的整屏静态图，单屏 showFullScreen 杜绝顶栏/Dock 压扁跳动）。"""
    sel = selector_cls(
        frozen_png=frozen_png,
        virtual_geometry=rect,
        mode_label=mode_label,
        on_selected=on_selected,
        on_cancelled=on_cancelled,
    )
    return _show_over_desktop(app, sel)


def open_translate_overlay(app, overlay_cls, png, rect, *, mode, virtual_rect, on_copy_text, on_retry, on_closed):
    """打开原位浮窗（全屏透明窗，与框选器同一显示方式，点框外由本窗消费并关闭）。"""
    ov = overlay_cls(
        png,
        rect,
        mode=mode,
        virtual_geometry=virtual_rect,
        on_copy_text=on_copy_text,
        on_retry=on_retry,
        on_closed=on_closed,
    )
    return _show_over_desktop(app, ov)


def open_annotator(
    app,
    annotator_cls,
    png,
    rect,
    *,
    virtual_rect,
    initial_style,
    on_style_changed,
    on_copy_image,
    on_save_image,
    on_closed,
):
    """打开截屏标注浮窗（全屏透明窗，与框选器/原位浮窗同一显示方式）。"""
    ann = annotator_cls(
        png,
        rect,
        virtual_geometry=virtual_rect,
        initial_style=initial_style,
        on_style_changed=on_style_changed,
        on_copy_image=on_copy_image,
        on_save_image=on_save_image,
        on_closed=on_closed,
    )
    return _show_over_desktop(app, ann)


def _show_over_desktop(app, widget):
    """单屏 showFullScreen（不被顶栏/Dock 压扁），多屏按虚拟桌面几何 show；置顶、激活并立即绘出首帧。"""
    if len(app.screens()) <= 1:
        widget.showFullScreen()
    else:
        widget.show()
    widget.raise_()
    widget.activateWindow()
    widget.repaint()
    return widget


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
