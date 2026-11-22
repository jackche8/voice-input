"""截屏标注工具条纯 QPainter 矢量图标绘制（对齐 docs/design/v3.5/annotate-icons-b.html 模板 B）。

单一职责：在调用方提供的 QPainter 局部 `[0, size] × [0, size]` 坐标系中，按 16×16 逻辑网格与
`theme.ANNOTATE_ICON_STROKE_PX`（1.6px）线宽绘制 9 枚工具/动作矢量图标（矩形、箭头、画笔、
文字、马赛克、撤销、复制剪贴板对勾、保存、取消）。纯几何绘制，零硬编码色值，零 `math` 依赖。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from _ui_desktop import theme

_ICON_VIEWBOX = 16.0
_MOSAIC_CELL_PX = 3.6
_MOSAIC_CELL_RADIUS = 0.4

_MOSAIC_SOLID_CELLS: tuple[tuple[float, float], ...] = (
    (2.6, 2.6),
    (9.8, 2.6),
    (6.2, 6.2),
    (2.6, 9.8),
    (9.8, 9.8),
)
_MOSAIC_DIM_CELLS: tuple[tuple[float, float], ...] = (
    (6.2, 2.6),
    (2.6, 6.2),
    (9.8, 6.2),
    (6.2, 9.8),
)


def _draw_rect(painter: QPainter, _color: QColor) -> None:
    painter.drawRoundedRect(QRectF(2.5, 3.5, 11.0, 9.0), 1.4, 1.4)


def _draw_arrow(painter: QPainter, _color: QColor) -> None:
    painter.drawLine(QPointF(3.0, 13.0), QPointF(12.5, 3.5))
    head = QPainterPath()
    head.moveTo(7.5, 3.5)
    head.lineTo(12.5, 3.5)
    head.lineTo(12.5, 8.5)
    painter.drawPath(head)


def _draw_brush(painter: QPainter, _color: QColor) -> None:
    body = QPainterPath()
    body.moveTo(10.9, 2.3)
    body.lineTo(13.7, 5.1)
    body.lineTo(5.9, 12.9)
    body.lineTo(2.5, 13.5)
    body.lineTo(3.1, 10.1)
    body.closeSubpath()
    painter.drawPath(body)
    painter.drawLine(QPointF(8.6, 4.6), QPointF(11.4, 7.4))


def _draw_text(painter: QPainter, _color: QColor) -> None:
    top = QPainterPath()
    top.moveTo(3.0, 4.8)
    top.lineTo(3.0, 3.0)
    top.lineTo(13.0, 3.0)
    top.lineTo(13.0, 4.8)
    painter.drawPath(top)
    painter.drawLine(QPointF(8.0, 3.0), QPointF(8.0, 13.0))
    painter.drawLine(QPointF(5.8, 13.0), QPointF(10.2, 13.0))


def _draw_mosaic(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for x, y in _MOSAIC_SOLID_CELLS:
        painter.drawRoundedRect(QRectF(x, y, _MOSAIC_CELL_PX, _MOSAIC_CELL_PX), _MOSAIC_CELL_RADIUS, _MOSAIC_CELL_RADIUS)
    painter.save()
    painter.setOpacity(painter.opacity() * theme.ANNOTATE_MOSAIC_DIM_OPACITY)
    for x, y in _MOSAIC_DIM_CELLS:
        painter.drawRoundedRect(QRectF(x, y, _MOSAIC_CELL_PX, _MOSAIC_CELL_PX), _MOSAIC_CELL_RADIUS, _MOSAIC_CELL_RADIUS)
    painter.restore()


def _draw_undo(painter: QPainter, _color: QColor) -> None:
    tip = QPainterPath()
    tip.moveTo(5.8, 3.5)
    tip.lineTo(2.8, 6.5)
    tip.lineTo(5.8, 9.5)
    painter.drawPath(tip)

    arc = QPainterPath()
    arc.moveTo(3.5, 6.5)
    arc.lineTo(9.5, 6.5)
    arc.arcTo(QRectF(6.25, 6.5, 6.5, 6.5), 90.0, -180.0)
    arc.lineTo(6.5, 13.0)
    painter.drawPath(arc)


def _draw_copy(painter: QPainter, _color: QColor) -> None:
    painter.drawRoundedRect(QRectF(5.6, 2.0, 4.8, 2.2), 0.7, 0.7)

    board = QPainterPath()
    board.moveTo(5.6, 3.1)
    board.lineTo(4.2, 3.1)
    board.arcTo(QRectF(3.0, 3.1, 2.4, 2.4), 90.0, 90.0)
    board.lineTo(3.0, 12.4)
    board.arcTo(QRectF(3.0, 11.2, 2.4, 2.4), 180.0, 90.0)
    board.lineTo(11.8, 13.6)
    board.arcTo(QRectF(10.6, 11.2, 2.4, 2.4), 270.0, 90.0)
    board.lineTo(13.0, 4.3)
    board.arcTo(QRectF(10.6, 3.1, 2.4, 2.4), 0.0, 90.0)
    board.lineTo(10.4, 3.1)
    painter.drawPath(board)

    check = QPainterPath()
    check.moveTo(5.6, 8.8)
    check.lineTo(7.3, 10.5)
    check.lineTo(10.5, 7.1)
    painter.drawPath(check)


def _draw_save(painter: QPainter, _color: QColor) -> None:
    painter.drawLine(QPointF(8.0, 2.5), QPointF(8.0, 10.2))

    head = QPainterPath()
    head.moveTo(4.8, 7.1)
    head.lineTo(8.0, 10.3)
    head.lineTo(11.2, 7.1)
    painter.drawPath(head)

    tray = QPainterPath()
    tray.moveTo(2.8, 10.8)
    tray.lineTo(2.8, 12.5)
    tray.arcTo(QRectF(2.8, 11.4, 2.2, 2.2), 180.0, 90.0)
    tray.lineTo(12.1, 13.6)
    tray.arcTo(QRectF(11.0, 11.4, 2.2, 2.2), 270.0, 90.0)
    tray.lineTo(13.2, 10.8)
    painter.drawPath(tray)


def _draw_cancel(painter: QPainter, _color: QColor) -> None:
    painter.drawLine(QPointF(4.0, 4.0), QPointF(12.0, 12.0))
    painter.drawLine(QPointF(12.0, 4.0), QPointF(4.0, 12.0))


_ICON_DRAWERS: dict[str, Callable[[QPainter, QColor], None]] = {
    "rect": _draw_rect,
    "arrow": _draw_arrow,
    "brush": _draw_brush,
    "text": _draw_text,
    "mosaic": _draw_mosaic,
    "undo": _draw_undo,
    "copy": _draw_copy,
    "save": _draw_save,
    "cancel": _draw_cancel,
}

ANNOTATE_ICON_KEYS: frozenset[str] = frozenset(_ICON_DRAWERS)


def draw_annotate_icon(
    painter: QPainter,
    icon_key: str,
    color: QColor,
    *,
    size: float = theme.ANNOTATE_ICON_GLYPH_PX,
) -> None:
    """按 16×16 逻辑网格在当前 painter 原点绘制指定标注矢量图标，非法键或非正尺寸立即抛 ValueError。"""
    drawer = _ICON_DRAWERS.get(icon_key) if isinstance(icon_key, str) else None
    if drawer is None:
        raise ValueError(f"不支持的标注图标键：{icon_key!r}，允许 {sorted(ANNOTATE_ICON_KEYS)}")
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size <= 0:
        raise ValueError(f"图标尺寸必须大于 0，收到 {size!r}")

    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = float(size) / _ICON_VIEWBOX
        if scale != 1.0:
            painter.scale(scale, scale)
        pen = QPen(
            color,
            theme.ANNOTATE_ICON_STROKE_PX,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        drawer(painter, color)
    finally:
        painter.restore()
