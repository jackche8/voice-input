"""语音一体底栏绘制（UI 验收标准 docs/design/v3.3/voice-a.html）。

纯绘制：三种形态（胶囊 / 字幕条 / 确认卡片）的尺寸计算，以及把某一时刻的 `BarFrame`
画出来——状态列、两行字幕（已确认 / 刚确认颜色过渡 / 尾巴虚线）、附加提示行与卡片按钮。
不持有状态、不读时钟、不含回调；颜色字号全部来自 `theme`。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from _ui_desktop import theme
from _ui_desktop.caption_bar import (
    CAPTION_LINE_GAP_PX,
    CAPTION_MAX_WIDTH_PX,
    CAPTION_MIN_WIDTH_PX,
    CAPTION_PAD_X_PX,
    CAPTION_PAD_Y_PX,
    wrap_lines,
)
from _ui_desktop.voice_bar_motion import mix

STATUS_W = 84
TEXT_PAD_L = 16
ICON_X = 18
LABEL_X = 30
CARD_LABEL_X = 32
PILL_H = 32
PILL_MAX_W = 560
PILL_CHROME_W = 46
CARD_H = 44
CARD_CHROME_W = 66
BUTTON_H = 28
BUTTON_PAD_X = 12
BUTTON_GAP = 8
CARD_RIGHT_PAD = 10
CARD_LABEL_GAP = 16
DIVIDER_INSET = 10
DOT_RADIUS = 4.0
SPIN_RADIUS = 5.5
UNDERLINE_DROP = 3.0
INNER_MAX_W = CAPTION_MAX_WIDTH_PX - 2 * CAPTION_PAD_X_PX
BAR_MAX_W = STATUS_W + TEXT_PAD_L + INNER_MAX_W + CAPTION_PAD_X_PX

RESEND = "resend"
IGNORE = "ignore"
BUTTON_LABELS = {RESEND: "重新输入", IGNORE: "忽略"}


class BarFonts:
    """一体底栏用到的全部字体与度量（构造一次复用）。"""

    def __init__(self) -> None:
        self.caption = theme.make_font(pixel_size=theme.FS_CAPTION, weight=QFont.Weight.Medium)
        self.small = theme.make_font(pixel_size=theme.FS_XS, weight=QFont.Weight.Medium)
        self.timer = theme.make_font(pixel_size=theme.FS_XS, mono=True, weight=QFont.Weight.Medium)
        self.card = theme.make_font(pixel_size=theme.FS_SM, weight=QFont.Weight.Medium)
        self.button = theme.make_font(pixel_size=theme.FS_XS, weight=QFont.Weight.DemiBold)
        self.caption_fm = QFontMetricsF(self.caption)
        self.line_h = float(round(self.caption_fm.height()))

    def bar_max_height(self) -> float:
        return 2 * CAPTION_PAD_Y_PX + 2 * self.line_h + CAPTION_LINE_GAP_PX


@dataclass(frozen=True)
class CaptionView:
    text: str
    spans: tuple[tuple[int, int], ...]
    stable_len: int
    fresh: tuple[int, int]
    fresh_mix: float
    roll: float
    opacity: float


@dataclass(frozen=True)
class NoticeView:
    text: str
    opacity: float
    rise: float


@dataclass(frozen=True)
class BarFrame:
    """某一时刻底栏的全部可见值（窗口内坐标）。"""

    form: str  # "pill" | "bar" | "card"
    rect: QRectF
    opacity: float
    content_opacity: float
    border: QColor
    icon: str  # "dot" | "spin" | "check"
    icon_color: QColor
    icon_opacity: float
    spin_deg: float
    check: float
    status_text: str
    status_mono: bool
    label: str
    caption: CaptionView | None
    notice: NoticeView | None
    hover: str | None


# ---------------------------------------------------------------------------
# 尺寸
# ---------------------------------------------------------------------------
def caption_spans(text: str, fonts: BarFonts) -> list[tuple[int, int]]:
    return wrap_lines(text, fonts.caption, INNER_MAX_W)


def bar_size(text: str, spans: list[tuple[int, int]], fonts: BarFonts) -> tuple[float, float]:
    visible = spans[-2:]
    widest = max(fonts.caption_fm.horizontalAdvance(text[s:e]) for s, e in visible)
    width = min(BAR_MAX_W, max(CAPTION_MIN_WIDTH_PX, STATUS_W + TEXT_PAD_L + widest + CAPTION_PAD_X_PX))
    height = 2 * CAPTION_PAD_Y_PX + len(visible) * fonts.line_h + (len(visible) - 1) * CAPTION_LINE_GAP_PX
    return width, height


def pill_size(label: str, fonts: BarFonts) -> tuple[float, float]:
    return min(PILL_MAX_W, PILL_CHROME_W + QFontMetricsF(fonts.small).horizontalAdvance(label)), PILL_H


def _button_width(key: str, fonts: BarFonts) -> float:
    return QFontMetricsF(fonts.button).horizontalAdvance(BUTTON_LABELS[key]) + 2 * BUTTON_PAD_X


def card_size(label: str, fonts: BarFonts) -> tuple[float, float]:
    label_w = QFontMetricsF(fonts.card).horizontalAdvance(label)
    width = label_w + _button_width(RESEND, fonts) + _button_width(IGNORE, fonts) + CARD_CHROME_W
    return min(BAR_MAX_W, width), CARD_H


def card_buttons(rect: QRectF, fonts: BarFonts) -> dict[str, QRectF]:
    """确认卡片两个按钮的区域（绘制与命中测试共用）。"""
    ignore_w = _button_width(IGNORE, fonts)
    resend_w = _button_width(RESEND, fonts)
    top = rect.center().y() - BUTTON_H / 2
    ignore_x = rect.right() - CARD_RIGHT_PAD - ignore_w
    resend_x = ignore_x - BUTTON_GAP - resend_w
    return {
        RESEND: QRectF(resend_x, top, resend_w, BUTTON_H),
        IGNORE: QRectF(ignore_x, top, ignore_w, BUTTON_H),
    }


# ---------------------------------------------------------------------------
# 绘制
# ---------------------------------------------------------------------------
def with_alpha(spec: theme.ColorSpec, alpha: int) -> QColor:
    """令牌色加指定 alpha（描边等半透明色）。"""
    color = theme.qcolor(spec, opaque=True)
    color.setAlpha(alpha)
    return color


def _baseline(fm: QFontMetricsF, center_y: float) -> float:
    return center_y + (fm.ascent() - fm.descent()) / 2


def paint_frame(painter: QPainter, frame: BarFrame, fonts: BarFonts) -> None:
    """画一帧：底板与描边 → 附加提示行 → 形态内容。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setOpacity(frame.opacity)
    rect = frame.rect
    radius = theme.RADIUS_LG if frame.form == "bar" else rect.height() / 2
    plate = QPainterPath()
    plate.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    fill = theme.CAPTION_BG_RGBA if frame.form == "bar" else theme.CAPSULE_BG_RGBA
    painter.fillPath(plate, theme.qcolor(fill))
    painter.strokePath(plate, QPen(frame.border, 1.0))
    if frame.notice is not None:
        _paint_notice(painter, rect, frame.notice, fonts)
    painter.setOpacity(frame.opacity * frame.content_opacity)
    if frame.form == "bar":
        _paint_bar(painter, frame, fonts)
    elif frame.form == "card":
        _paint_card(painter, frame, fonts)
    else:
        _paint_pill(painter, frame, fonts)
    painter.restore()


def _paint_icon(painter: QPainter, frame: BarFrame, cx: float, cy: float) -> None:
    pen = QPen(frame.icon_color, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if frame.icon == "spin":
        box = QRectF(cx - SPIN_RADIUS, cy - SPIN_RADIUS, 2 * SPIN_RADIUS, 2 * SPIN_RADIUS)
        painter.setPen(pen)
        painter.drawArc(box, int(-frame.spin_deg * 16), -270 * 16)
        return
    if frame.icon == "check":
        _paint_check(painter, pen, cx, cy, frame.check)
        return
    color = QColor(frame.icon_color)
    color.setAlphaF(color.alphaF() * frame.icon_opacity)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(cx, cy), DOT_RADIUS, DOT_RADIUS)


def _paint_check(painter: QPainter, pen: QPen, cx: float, cy: float, progress: float) -> None:
    """✓ 按长度比例逐段描出（出现动画）。"""
    segments = [
        QLineF(cx - 5.0, cy, cx - 1.5, cy + 3.5),
        QLineF(cx - 1.5, cy + 3.5, cx + 5.5, cy - 4.5),
    ]
    remaining = progress * sum(seg.length() for seg in segments)
    path = QPainterPath(segments[0].p1())
    for seg in segments:
        if remaining <= 0:
            break
        part = min(1.0, remaining / seg.length())
        path.lineTo(seg.pointAt(part))
        remaining -= seg.length()
    painter.strokePath(path, pen)


def _draw_text(painter: QPainter, font: QFont, color: QColor, x: float, center_y: float, text: str) -> None:
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(QPointF(x, _baseline(QFontMetricsF(font), center_y)), text)


def _paint_pill(painter: QPainter, frame: BarFrame, fonts: BarFonts) -> None:
    rect = frame.rect
    cy = rect.center().y()
    _paint_icon(painter, frame, rect.x() + ICON_X, cy)
    fm = QFontMetricsF(fonts.small)
    label = fm.elidedText(frame.label, Qt.TextElideMode.ElideRight, rect.width() - LABEL_X - 14)
    _draw_text(painter, fonts.small, theme.qcolor(theme.CAPSULE_TEXT_HEX), rect.x() + LABEL_X, cy, label)


def _paint_bar(painter: QPainter, frame: BarFrame, fonts: BarFonts) -> None:
    rect = frame.rect
    cy = rect.center().y()
    _paint_icon(painter, frame, rect.x() + ICON_X, cy)
    if frame.status_mono:
        status_font, status_color = fonts.timer, theme.qcolor(theme.CAPSULE_MUTED_HEX)
    else:
        status_font, status_color = fonts.small, frame.icon_color
    _draw_text(painter, status_font, status_color, rect.x() + LABEL_X, cy, frame.status_text)
    painter.fillRect(
        QRectF(rect.x() + STATUS_W, rect.y() + DIVIDER_INSET, 1.0, rect.height() - 2 * DIVIDER_INSET),
        theme.qcolor(theme.CAPSULE_BORDER_HEX),
    )
    if frame.caption is not None:
        painter.save()
        painter.setClipRect(QRectF(rect.x() + STATUS_W + 1, rect.y() + 3, rect.width() - STATUS_W - 4, rect.height() - 6))
        _paint_caption(painter, frame.caption, rect, fonts)
        painter.restore()


def _paint_caption(painter: QPainter, caption: CaptionView, rect: QRectF, fonts: BarFonts) -> None:
    """末尾两行；新行到来时整体从下往上卷一行，被顶出的行同时淡出。"""
    base_opacity = painter.opacity() * caption.opacity
    x0 = rect.x() + STATUS_W + TEXT_PAD_L
    y0 = rect.y() + CAPTION_PAD_Y_PX
    step = fonts.line_h + CAPTION_LINE_GAP_PX
    count = len(caption.spans)
    first_visible = max(0, count - 2)
    first = max(0, first_visible - (1 if caption.roll < 1.0 else 0))
    for index in range(first, count):
        top = y0 + (index - first_visible) * step + (1.0 - caption.roll) * step
        fading = 1.0 - caption.roll if index < first_visible else 1.0
        painter.save()
        painter.setOpacity(base_opacity * fading)
        _paint_caption_line(painter, caption, caption.spans[index], x0, top, fonts)
        painter.restore()


def _paint_caption_line(
    painter: QPainter,
    caption: CaptionView,
    span: tuple[int, int],
    x: float,
    top: float,
    fonts: BarFonts,
) -> None:
    """一行分三段：已确认（白）/ 刚确认（灰→白过渡）/ 未确认尾巴（灰 + 虚线下划线）。"""
    start, end = span
    fresh_start, fresh_end = caption.fresh
    cuts = {start, end} | {v for v in (fresh_start, fresh_end, caption.stable_len) if start < v < end}
    points = sorted(cuts)
    stable = theme.qcolor(theme.STABLE_TEXT_HEX, opaque=True)
    tail = theme.qcolor(theme.TAIL_TEXT_HEX, opaque=True)
    fm = fonts.caption_fm
    baseline = top + fm.ascent()
    painter.setFont(fonts.caption)
    for a, b in zip(points, points[1:]):
        piece = caption.text[a:b]
        width = fm.horizontalAdvance(piece)
        if a >= caption.stable_len:
            color = tail
        elif fresh_start <= a < fresh_end:
            color = mix(tail, stable, caption.fresh_mix)
        else:
            color = stable
        painter.setPen(color)
        painter.drawText(QPointF(x, baseline), piece)
        if a >= caption.stable_len:
            dash = QPen(tail, 1.0)
            dash.setDashPattern([3.0, 3.0])
            painter.setPen(dash)
            underline_y = baseline + UNDERLINE_DROP + 0.5
            painter.drawLine(QPointF(x, underline_y), QPointF(x + width, underline_y))
        x += width


def _paint_card(painter: QPainter, frame: BarFrame, fonts: BarFonts) -> None:
    rect = frame.rect
    cy = rect.center().y()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.qcolor(theme.STATE_WARN_HEX, opaque=True))
    painter.drawEllipse(QPointF(rect.x() + ICON_X, cy), DOT_RADIUS, DOT_RADIUS)
    buttons = card_buttons(rect, fonts)
    label_room = buttons[RESEND].x() - CARD_LABEL_GAP - (rect.x() + CARD_LABEL_X)
    fm = QFontMetricsF(fonts.card)
    label = fm.elidedText(frame.label, Qt.TextElideMode.ElideRight, label_room)
    _draw_text(painter, fonts.card, theme.qcolor(theme.CAPSULE_TEXT_HEX), rect.x() + CARD_LABEL_X, cy, label)
    for key, box in buttons.items():
        _paint_button(painter, key, box, frame.hover == key, fonts)


def _paint_button(painter: QPainter, key: str, box: QRectF, hovered: bool, fonts: BarFonts) -> None:
    primary = key == RESEND
    if primary:
        fill = theme.ACCENT_HOVER_HEX if hovered else theme.ACCENT_HEX
        border, text = theme.ACCENT_HEX, theme.BG_DARK_HEX
    else:
        fill = theme.SURFACE_2_HEX
        border = theme.ACCENT_HEX if hovered else theme.BORDER_HEX
        text = theme.ACCENT_HEX if hovered else theme.TEXT_PRIMARY_HEX
    path = QPainterPath()
    path.addRoundedRect(box, theme.RADIUS_SM, theme.RADIUS_SM)
    painter.fillPath(path, theme.qcolor(fill, opaque=True))
    painter.strokePath(path, QPen(theme.qcolor(border, opaque=True), 1.0))
    painter.setFont(fonts.button)
    painter.setPen(theme.qcolor(text, opaque=True))
    painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), BUTTON_LABELS[key])


def _paint_notice(painter: QPainter, rect: QRectF, notice: NoticeView, fonts: BarFonts) -> None:
    """附加提示行：底栏上沿外侧的标签，下边与底栏上沿相接（盖住那段 1px 描边），不画下边线。"""
    fm = QFontMetricsF(fonts.small)
    pad, dot, inset = theme.VOICE_NOTICE_PAD_X_PX, theme.VOICE_NOTICE_DOT_PX, theme.VOICE_NOTICE_INSET_PX
    width = pad + dot + dot + fm.horizontalAdvance(notice.text) + pad
    x = rect.x() + inset if width <= rect.width() - 2 * inset else rect.x() + (rect.width() - width) / 2
    bottom = rect.y() + 1
    top = rect.y() - theme.VOICE_NOTICE_HEIGHT_PX + 1 + notice.rise
    r = theme.VOICE_NOTICE_RADIUS_PX
    tab = QPainterPath(QPointF(x, bottom))
    tab.lineTo(x, top + r)
    tab.arcTo(QRectF(x, top, 2 * r, 2 * r), 180, -90)
    tab.lineTo(x + width - r, top)
    tab.arcTo(QRectF(x + width - 2 * r, top, 2 * r, 2 * r), 90, -90)
    tab.lineTo(x + width, bottom)
    painter.save()
    painter.setOpacity(painter.opacity() * notice.opacity)
    painter.fillPath(tab, theme.qcolor(theme.CAPTION_BG_RGBA))
    painter.strokePath(tab, QPen(with_alpha(theme.STATE_WARN_HEX, theme.VOICE_NOTICE_BORDER_ALPHA), 1.0))
    cy = (top + bottom) / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.qcolor(theme.STATE_WARN_HEX, opaque=True))
    painter.drawEllipse(QPointF(x + pad + dot / 2, cy), dot / 2, dot / 2)
    _draw_text(painter, fonts.small, theme.qcolor(theme.CAPSULE_TEXT_HEX), x + pad + 2 * dot, cy, notice.text)
    painter.restore()
