"""截屏标注工具数据模型、画笔平滑轨迹、重放绘制、马赛克块平均与原分辨率合成导出。

纯 UI 渲染辅助模块：不含业务流程、文件 IO 或剪贴板调用，所有颜色、线宽、字号与
马赛克块尺寸默认取自 `_ui_desktop.theme` 单源令牌。坐标统一使用选区画布逻辑坐标，
在导出与马赛克取样时按 `image_size / canvas_size` 换算到原冻结图像素坐标系，保证
HiDPI 下导出 PNG 分辨率严格等于原冻结图。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPolygonF

from _ui_desktop import theme

ToolName = Literal["rect", "arrow", "brush", "text", "mosaic"]

TOOL_RECT: ToolName = "rect"
TOOL_ARROW: ToolName = "arrow"
TOOL_BRUSH: ToolName = "brush"
TOOL_TEXT: ToolName = "text"
TOOL_MOSAIC: ToolName = "mosaic"

TOOL_ITEMS: tuple[tuple[ToolName, str], ...] = (
    (TOOL_RECT, "矩形"),
    (TOOL_ARROW, "箭头"),
    (TOOL_BRUSH, "画笔"),
    (TOOL_TEXT, "文字"),
    (TOOL_MOSAIC, "马赛克"),
)
VALID_TOOLS: frozenset[str] = frozenset(name for name, _label in TOOL_ITEMS)

ARROW_HEAD_BASE_PX = 8.0
ARROW_HEAD_STROKE_MULT = 4.0
ARROW_MIN_LENGTH_PX = 2.0
# tan(25°)，与 docs/design/v3.5/annotate-brush-b.html 箭头半角保持一致
ARROW_HALF_TAN = 0.4663076581549986

_DEFAULT_COLOR, _DEFAULT_SIZE = theme.resolve_annotate_style()


@dataclass(frozen=True)
class AnnotationOp:
    """单条标注操作（画布逻辑坐标 + 主题样式快照）。"""

    tool: ToolName
    start: QPointF
    end: QPointF
    points: tuple[QPointF, ...] = ()
    color: str = _DEFAULT_COLOR
    stroke_px: int = _DEFAULT_SIZE.stroke_px
    text_px: int = _DEFAULT_SIZE.text_px
    block_px: int = _DEFAULT_SIZE.block_px
    text: str = ""


def _point_distance(a: QPointF, b: QPointF) -> float:
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    return float((dx * dx + dy * dy) ** 0.5)


def make_op(
    tool: ToolName,
    start: QPointF,
    end: QPointF,
    *,
    color: str = theme.ANNOTATE_DEFAULT_COLOR_HEX,
    size_level: str = theme.ANNOTATE_DEFAULT_SIZE_LEVEL,
    points: Sequence[QPointF] = (),
    text: str = "",
) -> AnnotationOp:
    """使用主题单源校验后的颜色与粗细档位构建一条标注操作。"""
    if tool not in VALID_TOOLS:
        raise ValueError(f"不支持的标注工具：{tool!r}，允许 {sorted(VALID_TOOLS)}")
    validated_color, size_spec = theme.resolve_annotate_style(color, size_level)
    start_pt = QPointF(start)
    end_pt = QPointF(end)
    brush_pts: tuple[QPointF, ...] = ()
    if tool == TOOL_BRUSH:
        brush_pts = tuple(QPointF(p) for p in points) if points else (start_pt,)
        start_pt = brush_pts[0]
        end_pt = brush_pts[-1]
    return AnnotationOp(
        tool=tool,
        start=start_pt,
        end=end_pt,
        points=brush_pts,
        color=validated_color,
        stroke_px=size_spec.stroke_px,
        text_px=size_spec.text_px,
        block_px=size_spec.block_px,
        text=text,
    )


def append_brush_point(
    op: AnnotationOp,
    pt: QPointF,
    *,
    min_step_px: float = theme.ANNOTATE_BRUSH_MIN_STEP_PX,
    force: bool = False,
) -> AnnotationOp:
    """向画笔草稿追加采样点：常规拖拽按 min_step_px 去重，松手 force=True 补末点。"""
    if op.tool != TOOL_BRUSH:
        raise ValueError(f"append_brush_point 仅支持画笔工具，收到 {op.tool!r}")
    new_pt = QPointF(pt)
    if not op.points:
        return replace(op, start=new_pt, end=new_pt, points=(new_pt,))
    dist = _point_distance(op.points[-1], new_pt)
    if force:
        if dist <= 0.0:
            return op
    elif dist < float(min_step_px):
        return op
    return replace(op, end=new_pt, points=(*op.points, new_pt))


def brush_path_length(points: Sequence[QPointF]) -> float:
    """计算画笔轨迹总路径长度（逻辑像素）。"""
    if len(points) < 2:
        return 0.0
    total = 0.0
    for idx in range(1, len(points)):
        total += _point_distance(points[idx - 1], points[idx])
    return total


def build_brush_path(points: Sequence[QPointF]) -> QPainterPath:
    """将采样点列构造成中点二次贝塞尔平滑曲线（对齐 annotate-brush-b.html drawBrushPath）。"""
    path = QPainterPath()
    if not points:
        return path
    path.moveTo(points[0])
    if len(points) == 1:
        return path
    if len(points) == 2:
        path.lineTo(points[1])
        return path
    for idx in range(1, len(points) - 1):
        curr = points[idx]
        nxt = points[idx + 1]
        mid = QPointF((curr.x() + nxt.x()) / 2.0, (curr.y() + nxt.y()) / 2.0)
        path.quadTo(curr, mid)
    path.lineTo(points[-1])
    return path


def is_valid_op(op: AnnotationOp) -> bool:
    """判定标注操作是否有效：画笔按总路径长判有效以保留起终点重合的闭合圆。"""
    if op.tool == TOOL_TEXT:
        return bool(op.text.strip())
    if op.tool == TOOL_BRUSH:
        return len(op.points) >= 2 and brush_path_length(op.points) >= theme.ANNOTATE_BRUSH_MIN_PATH_PX
    rect = normalized_rect(op.start, op.end)
    return rect.width() + rect.height() > theme.ANNOTATE_MIN_DRAG_PX


def normalized_rect(start: QPointF, end: QPointF) -> QRectF:
    """将起止两点归一化为非负宽高的矩形。"""
    x0 = min(start.x(), end.x())
    y0 = min(start.y(), end.y())
    return QRectF(x0, y0, abs(end.x() - start.x()), abs(end.y() - start.y()))


def clamp_point(pt: QPointF, width: float, height: float) -> QPointF:
    """将画布坐标钳制在 `[0, width] × [0, height]` 内。"""
    return QPointF(
        max(0.0, min(float(width), pt.x())),
        max(0.0, min(float(height), pt.y())),
    )


def canvas_rect_to_image_rect(rect: QRectF, canvas_size: QSize, image_size: QSize) -> QRect:
    """将画布逻辑矩形映射到原冻结图像素矩形并裁切到图像边界内。"""
    cw = max(1, canvas_size.width())
    ch = max(1, canvas_size.height())
    iw = image_size.width()
    ih = image_size.height()
    sx = iw / cw
    sy = ih / ch
    x0 = max(0, min(iw, round(rect.left() * sx)))
    y0 = max(0, min(ih, round(rect.top() * sy)))
    x1 = max(0, min(iw, round(rect.right() * sx)))
    y1 = max(0, min(ih, round(rect.bottom() * sy)))
    return QRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def mosaic_patch(source: QImage, rect_px: QRect, block_w: int, block_h: int) -> QImage | None:
    """对图像像素矩形做块平均马赛克：Smooth 缩小求块均色 → Fast 最近邻放大成色块。"""
    clipped = rect_px.intersected(QRect(0, 0, source.width(), source.height()))
    w, h = clipped.width(), clipped.height()
    if w < 2 or h < 2:
        return None
    cols = max(1, round(w / max(1, int(block_w))))
    rows = max(1, round(h / max(1, int(block_h))))
    crop = source.copy(clipped)
    small = crop.scaled(
        cols,
        rows,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return small.scaled(
        w,
        h,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def draw_vector_op(painter: QPainter, op: AnnotationOp) -> None:
    """在当前画布坐标系中绘制单条矢量标注（矩形 / 箭头 / 画笔 / 文字）。"""
    color = theme.qcolor(op.color, opaque=True)
    if op.tool == TOOL_RECT:
        pen = QPen(
            color,
            float(op.stroke_px),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(normalized_rect(op.start, op.end))
        return

    if op.tool == TOOL_ARROW:
        dx = op.end.x() - op.start.x()
        dy = op.end.y() - op.start.y()
        length = float((dx * dx + dy * dy) ** 0.5)
        if length < ARROW_MIN_LENGTH_PX:
            return
        ux, uy = dx / length, dy / length
        head = min(length, ARROW_HEAD_STROKE_MULT * op.stroke_px + ARROW_HEAD_BASE_PX)
        half = ARROW_HALF_TAN * head
        bx = op.end.x() - ux * head
        by = op.end.y() - uy * head
        if length > head:
            pen = QPen(
                color,
                float(op.stroke_px),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(op.start, QPointF(bx, by))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        head_poly = QPolygonF(
            [
                QPointF(op.end.x(), op.end.y()),
                QPointF(bx - uy * half, by + ux * half),
                QPointF(bx + uy * half, by - ux * half),
            ]
        )
        painter.drawPolygon(head_poly)
        return

    if op.tool == TOOL_BRUSH:
        if not op.points:
            return
        if len(op.points) == 1:
            radius = max(1.5, op.stroke_px / 2.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(op.points[0], radius, radius)
            return
        pen = QPen(
            color,
            float(op.stroke_px),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(build_brush_path(op.points))
        return

    if op.tool == TOOL_TEXT:
        if not op.text:
            return
        font = theme.make_font(pixel_size=op.text_px, mono=False, weight=QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(color)
        fm = QFontMetricsF(font)
        painter.drawText(QPointF(op.start.x(), op.start.y() + fm.ascent()), op.text)
        return

    raise ValueError(f"draw_vector_op 仅支持矢量标注，收到 {op.tool!r}")


def apply_op_to_image(target: QImage, op: AnnotationOp, canvas_size: QSize) -> None:
    """将单条标注操作应用到原分辨率合成图（马赛克直接取样当前合成图再覆盖写回）。"""
    sx = target.width() / max(1, canvas_size.width())
    sy = target.height() / max(1, canvas_size.height())

    if op.tool == TOOL_MOSAIC:
        img_rect = canvas_rect_to_image_rect(normalized_rect(op.start, op.end), canvas_size, target.size())
        patch = mosaic_patch(
            target,
            img_rect,
            max(1, round(op.block_px * sx)),
            max(1, round(op.block_px * sy)),
        )
        if patch is None:
            return
        painter = QPainter(target)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(img_rect.topLeft(), patch)
        painter.end()
        return

    painter = QPainter(target)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(sx, sy)
    draw_vector_op(painter, op)
    painter.end()


def render_composite(base: QImage, ops: Sequence[AnnotationOp], canvas_size: QSize) -> QImage:
    """从原冻结图按顺序重放全部标注操作，返回与冻结图同分辨率的合成图。"""
    composite = base.copy()
    for op in ops:
        apply_op_to_image(composite, op, canvas_size)
    return composite


def paint_draft_op(painter: QPainter, draft: AnnotationOp, canvas: QRect, composite: QImage) -> None:
    """在浮窗上绘制拖拽中的实时草稿（马赛克取样当前合成图并叠虚线外框，矢量直接绘制）。"""
    painter.save()
    painter.setClipRect(QRectF(canvas))
    if draft.tool == TOOL_MOSAIC:
        norm = normalized_rect(draft.start, draft.end)
        img_rect = canvas_rect_to_image_rect(norm, canvas.size(), composite.size())
        sx = composite.width() / max(1, canvas.width())
        sy = composite.height() / max(1, canvas.height())
        patch = mosaic_patch(
            composite,
            img_rect,
            max(1, round(draft.block_px * sx)),
            max(1, round(draft.block_px * sy)),
        )
        if patch is not None:
            dest = QRectF(
                canvas.x() + img_rect.x() / sx,
                canvas.y() + img_rect.y() / sy,
                img_rect.width() / sx,
                img_rect.height() / sy,
            )
            painter.drawImage(dest, patch)
        dash_pen = QPen(theme.qcolor(theme.ACCENT_HEX, opaque=True), 1.0)
        dash_pen.setDashPattern(list(theme.ANNOTATE_DRAFT_DASH_PX))
        painter.setPen(dash_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(norm.translated(QPointF(canvas.topLeft())))
    else:
        painter.translate(QPointF(canvas.topLeft()))
        draw_vector_op(painter, draft)
    painter.restore()


def encode_png(image: QImage) -> bytes:
    """将 QImage 编码为无损 PNG 字节流。"""
    byte_array = QByteArray()
    buf = QBuffer(byte_array)
    if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("无法打开内存缓冲区编码 PNG")
    try:
        if not image.save(buf, "PNG"):
            raise RuntimeError("QImage 编码 PNG 失败")
    finally:
        buf.close()
    return bytes(byte_array.data())
