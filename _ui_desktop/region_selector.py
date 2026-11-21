"""跨平台全屏红色十字定位框选器（截屏原位翻译与纯 OCR 识别 100% 复用）。

纯 UI 组件：透明实时悬浮全屏框选（不预先全屏截图，杜绝屏幕跳动与压扁），选完后只回传全局逻辑
矩形 `(x, y, w, h)`，由上层按目标选区精确抓屏。不含抓屏命令或网络调用。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import (
    QCloseEvent,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QWidget

from _ui_desktop import theme

MIN_SELECTION_PX = 8
TAG_OFFSET_PX = 14
TAG_EDGE_MARGIN_PX = 6
RETICLE_RADIUS_PX = 5


class RedCrosshairSelector(QWidget):
    """全屏透明红色十字准星定位框选窗口。"""

    def __init__(
        self,
        virtual_geometry: tuple[int, int, int, int] | None = None,
        mode_label: str = "截屏翻译",
        on_selected: Callable[[tuple[int, int, int, int]], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode_label: str = mode_label or "截屏翻译"
        self._on_selected = on_selected
        self._on_cancelled = on_cancelled
        self._finished: bool = False

        vx, vy, vw, vh = theme.resolve_virtual_geometry(virtual_geometry)
        self._origin_x: int = vx
        self._origin_y: int = vy

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.setGeometry(vx, vy, vw, vh)
        self.resize(vw, vh)

        self._mouse_pos: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._drag_current: QPoint | None = None
        self._last_tag_rect: QRect = QRect()
        self._last_tag_text: str = ""

        self._tag_font: QFont = theme.make_font(pixel_size=theme.FS_MONO_TAG, mono=True)
        self._hint_font: QFont = theme.make_font(
            pixel_size=theme.FS_XS,
            mono=False,
            weight=QFont.Weight.DemiBold,
        )

    @property
    def mouse_position(self) -> QPoint | None:
        """当前窗口逻辑坐标系内的光标位置；未收到真实指针事件前为 None。"""
        if self._mouse_pos is None:
            return None
        return QPoint(self._mouse_pos)

    def selection_rect(self) -> QRect | None:
        """正在拖拽时的归一化窗口内逻辑选区；未拖拽时返回 None。"""
        if self._drag_start is None or self._drag_current is None:
            return None
        return self._normalized_logical_rect(self._drag_start, self._drag_current)

    def coord_tag_rect(self) -> QRect | None:
        """返回当前光标位置对应的坐标/尺寸胶囊矩形（已做窗口边缘防裁切钳制）；未收到指针事件前返回 None。"""
        if self._mouse_pos is None:
            return None
        w = max(1, self.width())
        h = max(1, self.height())
        mx = max(0, min(self._mouse_pos.x(), w - 1))
        my = max(0, min(self._mouse_pos.y(), h - 1))
        text = self._format_coord_tag(mx, my, self.selection_rect())
        fm = QFontMetrics(self._tag_font)
        tag_w = fm.horizontalAdvance(text) + 16
        tag_h = fm.height() + 8
        return self.compute_tag_rect(mx, my, tag_w, tag_h, w, h)

    def coord_tag_text(self) -> str:
        if self._mouse_pos is None:
            return ""
        w = max(1, self.width())
        h = max(1, self.height())
        mx = max(0, min(self._mouse_pos.x(), w - 1))
        my = max(0, min(self._mouse_pos.y(), h - 1))
        return self._format_coord_tag(mx, my, self.selection_rect())

    @staticmethod
    def compute_tag_rect(
        mx: int,
        my: int,
        tag_w: int,
        tag_h: int,
        canvas_w: int,
        canvas_h: int,
    ) -> QRect:
        """计算跟随光标的胶囊矩形：靠近右/下边界时自动翻转到左/上方并严格钳制在画布内。"""
        margin = TAG_EDGE_MARGIN_PX
        tx = mx + TAG_OFFSET_PX
        ty = my + TAG_OFFSET_PX
        if tx + tag_w + margin > canvas_w:
            tx = mx - TAG_OFFSET_PX - tag_w
        if ty + tag_h + margin > canvas_h:
            ty = my - TAG_OFFSET_PX - tag_h

        max_x = max(margin, canvas_w - tag_w - margin)
        max_y = max(margin, canvas_h - tag_h - margin)
        tx = max(0, min(max(margin, tx), max_x))
        ty = max(0, min(max(margin, ty), max_y))
        clamped_w = min(tag_w, canvas_w)
        clamped_h = min(tag_h, canvas_h)
        return QRect(tx, ty, clamped_w, clamped_h)

    def _normalized_logical_rect(self, p0: QPoint, p1: QPoint) -> QRect:
        w = max(1, self.width())
        h = max(1, self.height())
        x0 = max(0, min(p0.x(), w))
        y0 = max(0, min(p0.y(), h))
        x1 = max(0, min(p1.x(), w))
        y1 = max(0, min(p1.y(), h))
        rx = min(x0, x1)
        ry = min(y0, y1)
        rw = abs(x1 - x0)
        rh = abs(y1 - y0)
        return QRect(rx, ry, rw, rh)

    def _format_coord_tag(self, mx: int, my: int, sel: QRect | None) -> str:
        if sel is not None and (sel.width() > 0 or sel.height() > 0):
            gx = self._origin_x + sel.x()
            gy = self._origin_y + sel.y()
            return f"{sel.width()} × {sel.height()} px  ({gx}, {gy})"
        gx = self._origin_x + mx
        gy = self._origin_y + my
        return f"X: {gx}, Y: {gy}"

    def paintEvent(self, _event: QPaintEvent) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        # 1. 未选区暗色半透明遮罩，选区内部完全透明直接透出真实桌面（0 跳动、0 缩放失真）
        dim_color = theme.qcolor(theme.DIM_MASK_RGBA)
        sel = self.selection_rect()
        if sel is None or sel.width() <= 0 or sel.height() <= 0:
            painter.fillRect(QRect(0, 0, w, h), dim_color)
        else:
            sx, sy, sw, sh = sel.x(), sel.y(), sel.width(), sel.height()
            if sy > 0:
                painter.fillRect(QRect(0, 0, w, sy), dim_color)
            if sy + sh < h:
                painter.fillRect(QRect(0, sy + sh, w, h - (sy + sh)), dim_color)
            if sx > 0:
                painter.fillRect(QRect(0, sy, sx, sh), dim_color)
            if sx + sw < w:
                painter.fillRect(QRect(sx + sw, sy, w - (sx + sw), sh), dim_color)

            # 选区 1px 红色高亮边框
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            sel_pen = QPen(theme.qcolor(theme.CROSSHAIR_HEX, opaque=True), 1)
            painter.setPen(sel_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRect(sx, sy, max(0, sw - 1), max(0, sh - 1)))

        # 3. 顶部模式提示胶囊（未拖拽时展示，区分「截屏翻译」与「纯 OCR」）
        if sel is None and w >= 240 and h >= 80:
            self._draw_top_hint(painter, w)

        if self._mouse_pos is None:
            painter.end()
            return

        # 4. 贯穿全屏宽高的 1px #ff2d2d 红色十字定位线 + 中心准星
        mx = max(0, min(self._mouse_pos.x(), w - 1))
        my = max(0, min(self._mouse_pos.y(), h - 1))
        crosshair_color = theme.qcolor(theme.CROSSHAIR_HEX, opaque=True)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        line_pen = QPen(crosshair_color, 1)
        painter.setPen(line_pen)
        painter.drawLine(0, my, w - 1, my)
        painter.drawLine(mx, 0, mx, h - 1)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        reticle_pen = QPen(crosshair_color, 1.2)
        painter.setPen(reticle_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(
            QRectF(
                mx - RETICLE_RADIUS_PX,
                my - RETICLE_RADIUS_PX,
                RETICLE_RADIUS_PX * 2,
                RETICLE_RADIUS_PX * 2,
            )
        )

        # 5. 跟随坐标/尺寸胶囊（防边缘裁切）
        self._draw_coord_tag(painter, mx, my, w, h, sel)
        painter.end()

    def _draw_top_hint(self, painter: QPainter, canvas_w: int) -> None:
        hint_text = f"{self.mode_label} · 移动鼠标红色十字线定位 · 左键拖拽框选 · 右键 / Esc 取消"
        painter.setFont(self._hint_font)
        fm = QFontMetrics(self._hint_font)
        pill_w = min(canvas_w - 20, fm.horizontalAdvance(hint_text) + 32)
        pill_h = fm.height() + 12
        pill_x = max(10, (canvas_w - pill_w) // 2)
        pill_rect = QRectF(pill_x, 14, pill_w, pill_h)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        radius = pill_h / 2.0
        path.addRoundedRect(pill_rect, radius, radius)
        painter.fillPath(path, theme.qcolor(theme.TOP_HINT_BG_RGBA))
        painter.setPen(QPen(theme.qcolor(theme.TOP_HINT_BORDER_HEX), 1))
        painter.drawPath(path)

        painter.setPen(theme.qcolor(theme.TOP_HINT_TEXT_HEX, opaque=True))
        elided = fm.elidedText(hint_text, Qt.TextElideMode.ElideRight, int(pill_w - 24))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, elided)

    def _draw_coord_tag(
        self,
        painter: QPainter,
        mx: int,
        my: int,
        canvas_w: int,
        canvas_h: int,
        sel: QRect | None,
    ) -> None:
        tag_text = self._format_coord_tag(mx, my, sel)
        painter.setFont(self._tag_font)
        fm = QFontMetrics(self._tag_font)
        tag_w = fm.horizontalAdvance(tag_text) + 16
        tag_h = fm.height() + 8
        tag_rect = self.compute_tag_rect(mx, my, tag_w, tag_h, canvas_w, canvas_h)
        self._last_tag_rect = tag_rect
        self._last_tag_text = tag_text

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(tag_rect), 4.0, 4.0)
        painter.fillPath(path, theme.qcolor(theme.COORD_TAG_BG_RGBA))
        painter.setPen(QPen(theme.qcolor(theme.COORD_TAG_BORDER_HEX, opaque=True), 1))
        painter.drawPath(path)

        painter.setPen(theme.qcolor(theme.COORD_TAG_TEXT_HEX, opaque=True))
        painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag_text)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._mouse_pos = event.position().toPoint()
        self.update()
        super().enterEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        self._mouse_pos = pos
        if self._drag_start is not None:
            self._drag_current = pos
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        self._mouse_pos = pos
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = pos
            self._drag_current = pos
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        self._mouse_pos = pos
        if event.button() != Qt.MouseButton.LeftButton or self._drag_start is None:
            super().mouseReleaseEvent(event)
            return

        sel = self._normalized_logical_rect(self._drag_start, pos)
        self._drag_start = None
        self._drag_current = None
        if sel.width() < MIN_SELECTION_PX or sel.height() < MIN_SELECTION_PX:
            self.update()
            event.accept()
            return

        self._complete_selection(sel)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def _complete_selection(self, sel: QRect) -> None:
        if self._finished:
            return
        self._finished = True

        platform_name = (QGuiApplication.platformName() or "").lower()
        if platform_name not in {"offscreen", "minimal"} and self.isVisible():
            top_left = self.mapToGlobal(sel.topLeft())
            global_rect = (top_left.x(), top_left.y(), sel.width(), sel.height())
        else:
            global_rect = (
                self._origin_x + sel.x(),
                self._origin_y + sel.y(),
                sel.width(),
                sel.height(),
            )
        cb = self._on_selected
        self.hide()
        self.close()
        if cb is not None:
            cb(global_rect)

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        cb = self._on_cancelled
        self.hide()
        self.close()
        if cb is not None:
            cb()

    def close_silent(self) -> None:
        """强制关闭选区窗口而不触发取消回调（例如新一次框选覆盖旧窗口）。"""
        self._finished = True
        self.hide()
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._finished:
            self._finished = True
            cb = self._on_cancelled
            if cb is not None:
                cb()
        super().closeEvent(event)
