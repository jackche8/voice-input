"""原位识别 / 翻译浮窗（UI 验收标准 docs/design/v3.3/overlay-a.html「贴合原图」）。

单一职责：与框选器同一形态的全屏透明窗口——在选区位置画截图、每块 86% 采样底色贴片与逐行文字
（OCR 模式画回原行框，翻译模式按段首槽位排版，见 `inplace_layout.py`），框下迷你菜单见 `mini_menu.py`；
点框外空白（点击在本窗消费，不到下层程序）或 Esc 关闭，其它键（含 Ctrl+C）不关。
纯 UI：不含网络、OCR、剪贴板，也不自行拼接全文。
"""
from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QWidget

from _contracts import OcrTextBlock
from _ui_desktop import theme
from _ui_desktop.inplace_layout import BlockLayout, layout_ocr_block, layout_translated_block, row_font
from _ui_desktop.mini_menu import COPY_OCR, COPY_TRANSLATED, MiniMenu, place_menu
from _ui_desktop.voice_bar_motion import FrameTicker, ease_out

MODES = ("translate", "ocr")
FAILURE_PREFIX = {"translate": "翻译失败", "ocr": "识别失败"}


class InplaceTranslateOverlay(QWidget):
    """全屏透明原位浮窗：截图 + 半透明贴片 + 逐行文字 + 框下迷你菜单。"""

    def __init__(
        self,
        cropped_png: bytes,
        screen_rect: tuple[int, int, int, int],
        *,
        mode: str,
        virtual_geometry: tuple[int, int, int, int] | None = None,
        on_copy_text: Callable[[str], bool] | None = None,
        on_retry: Callable[[], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        available_screen_rect: tuple[int, int, int, int] | None = None,
        copy_feedback_ms: int = 1500,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in MODES:
            raise ValueError(f"mode 只能是 translate / ocr，收到 {mode!r}")
        if not cropped_png:
            raise ValueError("cropped_png 不能为空")
        base = QImage.fromData(cropped_png)
        if base.isNull() or base.width() <= 0 or base.height() <= 0:
            raise ValueError("cropped_png 不是有效的图像数据")
        sx, sy, sw, sh = (int(v) for v in screen_rect)
        if sw <= 0 or sh <= 0:
            raise ValueError(f"screen_rect 宽高必须大于 0，收到 {screen_rect!r}")
        vx, vy, vw, vh = theme.resolve_virtual_geometry(virtual_geometry)

        self._mode = mode
        self._base = self._ensure_opaque_base(base)
        self._screen_rect = QRect(sx, sy, sw, sh)
        self._origin = QPoint(vx, vy)
        self._canvas = QRect(sx - vx, sy - vy, sw, sh)
        self._avail = theme.resolve_available_screen_rect(available_screen_rect, QPoint(sx, sy))
        self._on_copy_text = on_copy_text
        self._on_retry = on_retry
        self._on_closed = on_closed
        self._closed_notified = False
        self._phase = "loading"
        self._opened_at = self._now()
        self._phase_at = self._opened_at
        self._layouts: list[tuple[OcrTextBlock, BlockLayout]] = []
        self._full_translated_text = ""
        self._full_ocr_text = ""
        self._menu_placement = "below"

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(vx, vy, vw, vh)

        self.menu = MiniMenu(mode, on_copy=self._copy, on_retry=self._retry, feedback_ms=copy_feedback_ms, parent=self)
        self._ticker = FrameTicker(self._on_frame, self)
        self.set_loading()

    @staticmethod
    def _ensure_opaque_base(image: QImage) -> QImage:
        converted = image.convertToFormat(QImage.Format.Format_ARGB32)
        opaque = QImage(converted.size(), QImage.Format.Format_ARGB32)
        opaque.fill(theme.qcolor(theme.DEFAULT_BLOCK_BG_HEX, opaque=True))
        painter = QPainter(opaque)
        painter.drawImage(0, 0, converted)
        painter.end()
        return opaque

    # ------------------------------------------------------------------
    # 公共接口（capture_flow.OverlayHandle）
    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def menu_placement(self) -> str:
        """迷你菜单当前位置：below / above / inside。"""
        return self._menu_placement

    def canvas_rect(self) -> QRect:
        """选区画布在本窗内的逻辑矩形。"""
        return QRect(self._canvas)

    def set_loading(self) -> None:
        """识别 / 翻译中：外框虚线流动，菜单转圈。"""
        self._enter_phase("loading")
        self.menu.show_loading()
        self._place_menu()

    def set_result(self, blocks: list[OcrTextBlock], full_translated_text: str, full_ocr_text: str) -> None:
        """显示结果；翻译模式下某块译文行数与硬行段数不符时抛 ValueError（契约违例）。"""
        layout_block = layout_ocr_block if self._mode == "ocr" else layout_translated_block
        layouts = [(block, layout_block(block)) for block in blocks]
        for block, _layout in layouts:
            self._block_color(block.bg_color, "bg_color")
            self._block_color(block.fg_color, "fg_color")
        self._layouts = layouts
        self._full_translated_text = full_translated_text
        self._full_ocr_text = full_ocr_text
        self._enter_phase("result")
        copyable = {COPY_OCR: bool(full_ocr_text), COPY_TRANSLATED: bool(full_translated_text)}
        self.menu.show_result({key: copyable[key] for key in self._copy_keys()})
        self._place_menu()

    def set_error(self, message: str) -> None:
        """失败：外框转红，菜单显示原因 +「重试」。"""
        prefix = FAILURE_PREFIX[self._mode]
        self._enter_phase("error")
        self.menu.show_error(f"{prefix}：{message}" if message else prefix)
        self._place_menu()

    def close_silent(self) -> None:
        """关闭但不回调 on_closed（被新一次截屏取代时）。"""
        self._closed_notified = True
        self.close()

    # ------------------------------------------------------------------
    # 内部：状态、菜单与动作
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> float:
        return time.monotonic() * 1000.0

    @staticmethod
    def _block_color(value: str, field: str) -> QColor:
        color = QColor(value)
        if not color.isValid():
            raise ValueError(f"OcrTextBlock.{field} 不是有效颜色：{value!r}")
        return color

    def _copy_keys(self) -> tuple[str, ...]:
        return (COPY_OCR,) if self._mode == "ocr" else (COPY_TRANSLATED, COPY_OCR)

    def _enter_phase(self, phase: str) -> None:
        self._phase = phase
        self._phase_at = self._now()
        self._kick()

    def _place_menu(self) -> None:
        self.menu.adjustSize()
        top_left, placement = place_menu(self._screen_rect, self.menu.size(), self._avail)
        self.menu.move(top_left - self._origin)
        self._menu_placement = placement

    def _copy(self, key: str) -> bool:
        if self._on_copy_text is None:
            return False
        text = self._full_translated_text if key == COPY_TRANSLATED else self._full_ocr_text
        return bool(self._on_copy_text(text))

    def _retry(self) -> None:
        if self._on_retry is not None:
            self._on_retry()

    # ------------------------------------------------------------------
    # 帧驱动与绘制
    # ------------------------------------------------------------------
    def _kick(self) -> None:
        self.update()
        self._ticker.schedule(theme.OVERLAY_FRAME_MS)

    def _on_frame(self) -> None:
        self.update()
        t = self._now()
        fading = t < self._opened_at + theme.OVERLAY_OPEN_FADE_MS or (
            self._phase == "result" and t < self._phase_at + theme.OVERLAY_RESULT_FADE_MS
        )
        self._ticker.schedule(theme.OVERLAY_FRAME_MS if self._phase == "loading" or fading else None)

    def paintEvent(self, _event: QPaintEvent) -> None:
        t = self._now()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(ease_out(t, self._opened_at, theme.OVERLAY_OPEN_FADE_MS))
        painter.drawImage(QRectF(self._canvas), self._base)
        self._paint_outline(painter, t)
        if self._phase == "result":
            painter.save()
            painter.setOpacity(painter.opacity() * ease_out(t, self._phase_at, theme.OVERLAY_RESULT_FADE_MS))
            painter.translate(QPointF(self._canvas.topLeft()))
            painter.scale(self._canvas.width() / self._base.width(), self._canvas.height() / self._base.height())
            self._paint_blocks(painter)
            painter.restore()
        painter.end()

    def _paint_outline(self, painter: QPainter, t: float) -> None:
        width = theme.OVERLAY_OUTLINE_WIDTH_PX
        color = theme.ERROR_HEX if self._phase == "error" else theme.OVERLAY_OUTLINE_HEX
        pen = QPen(theme.qcolor(color, opaque=True), width)
        if self._phase == "loading":
            dash, gap = theme.OVERLAY_DASH_PATTERN_PX
            pen.setDashPattern([dash / width, gap / width])
            pen.setDashOffset(-(t * theme.OVERLAY_ANTS_SPEED_PX_S / 1000.0) / width)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = width / 2
        painter.drawRect(QRectF(self._canvas).adjusted(-half, -half, half, half))

    def _paint_blocks(self, painter: QPainter) -> None:
        """两遍：先全部 86% 采样底色贴片，再全部前景文字（相邻块的贴片不会盖住彼此的字）。"""
        pad_x, pad_y = theme.OVERLAY_PATCH_PAD_X_PX, theme.OVERLAY_PATCH_PAD_Y_PX
        radius = theme.OVERLAY_PATCH_RADIUS_PX
        for block, layout in self._layouts:
            fill = self._block_color(block.bg_color, "bg_color")
            fill.setAlpha(theme.OVERLAY_PATCH_ALPHA)
            patch = QPainterPath()
            patch.addRoundedRect(layout.extent.adjusted(-pad_x, -pad_y, pad_x, pad_y), radius, radius)
            painter.fillPath(patch, fill)
        for block, layout in self._layouts:
            painter.setPen(self._block_color(block.fg_color, "fg_color"))
            for row in layout.rows:
                painter.setFont(row_font(row))
                painter.drawText(QPointF(row.x, row.baseline), row.text)

    # ------------------------------------------------------------------
    # 关闭：点框外空白 / Esc
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        pos = event.position().toPoint()
        if self._canvas.contains(pos) or self.menu.geometry().contains(pos):
            return
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.close()
            return
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._ticker.stop()
        if not self._closed_notified:
            self._closed_notified = True
            if self._on_closed is not None:
                self._on_closed()
        super().closeEvent(event)
