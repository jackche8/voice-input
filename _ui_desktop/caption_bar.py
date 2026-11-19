"""屏幕底部居中双行流式大字幕浮窗（方案 C）。

单一职责：
1. 使用 `QTextLayout` 对 `stable_text + tail_text` 进行像素级断行，并将 Qt UTF-16
   code unit 偏移精确映射回 Python Unicode 码点下标（兼容 Emoji 与 CJK 扩展区代理对）；
2. 过滤空白断行后严格只截取最新末尾两行渲染，彻底消除多行堆叠溢出；
3. 同屏区分 `stable_text` (`#f8fafc`) 与 `tail_text` (`#94a3b8`)，完成后 1.5s 自动隐藏；
4. 恒定鼠标穿透且不抢夺输入焦点。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget

from _ui_desktop import theme

HUD_BOTTOM_MARGIN_PX = 28
CAPTION_MIN_WIDTH_PX = 480
CAPTION_MAX_WIDTH_PX = 760
CAPTION_PAD_X_PX = 22
CAPTION_PAD_Y_PX = 12
CAPTION_LINE_GAP_PX = 6
CAPTION_AUTO_HIDE_MS = 1500

PASSIVE_WINDOW_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.WindowTransparentForInput
)


def passive_overlay_flags() -> Qt.WindowType:
    """被动浮窗标志：Linux xcb 下启用 BypassWindowManagerHint，杜绝 Mutter 堆叠闪烁与坐标漂移。"""
    flags = PASSIVE_WINDOW_FLAGS
    if (QGuiApplication.platformName() or "").lower() == "xcb":
        flags |= Qt.WindowType.BypassWindowManagerHint
    return flags


def normalize_single_line(text: str) -> str:
    """将换行符归一化为空格，使单次语音流式文本按字幕栏宽度连续自动断行。"""
    if not text:
        return ""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _build_utf16_to_codepoint_map(text: str) -> list[int]:
    """构建 Qt QString UTF-16 code unit 偏移到 Python str 码点下标的精确映射表。"""
    u16_to_py: list[int] = []
    for py_idx, ch in enumerate(text):
        u16_to_py.append(py_idx)
        if ord(ch) > 0xFFFF:
            # 补充平面字符在 UTF-16 中占高低代理对（2 个 code unit）
            u16_to_py.append(py_idx)
    u16_to_py.append(len(text))
    return u16_to_py


def compute_trailing_two_lines(
    stable_text: str,
    tail_text: str,
    max_line_width: int,
    font: QFont,
) -> list[tuple[str, str]]:
    """对 stable_text + tail_text 执行像素级断行，严格只返回最新末尾两行 (stable_seg, tail_seg)。"""
    stable_norm = normalize_single_line(stable_text)
    tail_norm = normalize_single_line(tail_text)
    combined = stable_norm + tail_norm
    if not combined.strip():
        return []

    stable_end = len(stable_norm)
    u16_to_py = _build_utf16_to_codepoint_map(combined)
    max_u16 = len(u16_to_py) - 1
    width = max(40, int(max_line_width))

    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

    layout = QTextLayout(combined, font)
    layout.setTextOption(option)
    layout.beginLayout()
    py_spans: list[tuple[int, int]] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        u16_start = max(0, min(line.textStart(), max_u16))
        u16_end = max(u16_start, min(line.textStart() + line.textLength(), max_u16))
        py_start = u16_to_py[u16_start]
        py_end = u16_to_py[u16_end]
        if py_end > py_start and combined[py_start:py_end].strip():
            py_spans.append((py_start, py_end))
    layout.endLayout()

    trailing = py_spans[-2:]
    result: list[tuple[str, str]] = []
    for start, end in trailing:
        s_start = min(start, stable_end)
        s_end = min(end, stable_end)
        t_start = max(start, stable_end)
        t_end = max(end, stable_end)
        result.append((combined[s_start:s_end], combined[t_start:t_end]))
    return result


class StreamingCaptionBar(QWidget):
    """屏幕底部居中双行流式大字幕浮窗（恒定鼠标穿透、不抢焦点）。"""

    def __init__(
        self,
        *,
        available_screen_rect: tuple[int, int, int, int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._available_screen_override = available_screen_rect
        self._enabled: bool = True
        self._stable_text: str = ""
        self._tail_text: str = ""
        self._visible_lines: list[tuple[str, str]] = []

        self._font: QFont = theme.make_font(
            pixel_size=theme.FS_CAPTION,
            weight=QFont.Weight.Medium,
        )

        self.setWindowFlags(passive_overlay_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.clear)
        self.hide()

    @property
    def stable_text(self) -> str:
        return self._stable_text

    @property
    def tail_text(self) -> str:
        return self._tail_text

    def visible_lines(self) -> list[tuple[str, str]]:
        """返回当前实际渲染的末尾字幕行列表（长度恒 <= 2）。"""
        return list(self._visible_lines)

    def is_auto_hide_active(self) -> bool:
        return self._auto_hide_timer.isActive()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self.hide()
        elif self._visible_lines:
            self.show()

    def set_streaming_text(self, stable_text: str = "", tail_text: str = "") -> None:
        """更新实时流式字幕并立即展示（取消未到期的自动隐藏定时器）。"""
        self._auto_hide_timer.stop()
        self._stable_text = stable_text or ""
        self._tail_text = tail_text or ""
        self._relayout()
        if not self._visible_lines or not self._enabled:
            self.hide()
        else:
            if not self.isVisible():
                self.show()
            self.update()

    def finish(
        self,
        final_text: str | None = None,
        hold_ms: int = CAPTION_AUTO_HIDE_MS,
    ) -> None:
        """流式识别结束：定格最终文本并在 hold_ms (默认 1.5s) 后自动隐藏。"""
        if final_text is not None:
            self._stable_text = final_text
            self._tail_text = ""
        elif self._tail_text:
            self._stable_text = f"{self._stable_text}{self._tail_text}"
            self._tail_text = ""

        self._relayout()
        if not self._visible_lines or not self._enabled:
            self.clear()
            return

        if not self.isVisible():
            self.show()
        self.update()
        self._auto_hide_timer.start(max(1, int(hold_ms)))

    def clear(self) -> None:
        """立即清空并隐藏字幕条。"""
        self._auto_hide_timer.stop()
        self._stable_text = ""
        self._tail_text = ""
        self._visible_lines = []
        self.hide()

    def _relayout(self) -> None:
        avail = theme.resolve_available_screen_rect(self._available_screen_override)
        max_bar_w = min(CAPTION_MAX_WIDTH_PX, max(240, avail.width() - 40))
        min_bar_w = min(CAPTION_MIN_WIDTH_PX, max_bar_w)
        inner_max_w = max(80, max_bar_w - 2 * CAPTION_PAD_X_PX)

        self._visible_lines = compute_trailing_two_lines(
            self._stable_text,
            self._tail_text,
            inner_max_w,
            self._font,
        )
        if not self._visible_lines:
            return

        fm = QFontMetrics(self._font)
        widest_line = max(
            fm.horizontalAdvance(s_seg) + fm.horizontalAdvance(t_seg)
            for s_seg, t_seg in self._visible_lines
        )
        bar_w = max(min_bar_w, min(max_bar_w, widest_line + 2 * CAPTION_PAD_X_PX))
        line_count = len(self._visible_lines)
        line_h = fm.height()
        bar_h = (
            2 * CAPTION_PAD_Y_PX
            + line_count * line_h
            + max(0, line_count - 1) * CAPTION_LINE_GAP_PX
        )

        bar_x = avail.x() + (avail.width() - bar_w) // 2
        bar_y = avail.y() + avail.height() - bar_h - HUD_BOTTOM_MARGIN_PX
        target = QRect(bar_x, bar_y, bar_w, bar_h)
        if self.geometry() != target:
            self.setGeometry(target)

    def paintEvent(self, _event: QPaintEvent) -> None:
        if not self._visible_lines:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect_f = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        path = QPainterPath()
        path.addRoundedRect(rect_f, theme.RADIUS_LG, theme.RADIUS_LG)
        painter.fillPath(path, theme.qcolor(theme.CAPTION_BG_RGBA))
        painter.setPen(QPen(theme.qcolor(theme.CAPTION_BORDER_RGBA), 1.0))
        painter.drawPath(path)

        painter.setFont(self._font)
        fm = QFontMetrics(self._font)
        line_h = fm.height()
        stable_color = theme.qcolor(theme.STABLE_TEXT_HEX, opaque=True)
        tail_color = theme.qcolor(theme.TAIL_TEXT_HEX, opaque=True)
        underline_pen = QPen(tail_color, 1.0, Qt.PenStyle.DashLine)

        for idx, (stable_seg, tail_seg) in enumerate(self._visible_lines):
            s_w = fm.horizontalAdvance(stable_seg)
            t_w = fm.horizontalAdvance(tail_seg)
            total_w = s_w + t_w
            start_x = max(CAPTION_PAD_X_PX, int((w - total_w) / 2))
            top_y = CAPTION_PAD_Y_PX + idx * (line_h + CAPTION_LINE_GAP_PX)
            baseline_y = top_y + fm.ascent()

            if stable_seg:
                painter.setPen(stable_color)
                painter.drawText(QPoint(start_x, baseline_y), stable_seg)
            if tail_seg:
                tail_x = start_x + s_w
                painter.setPen(tail_color)
                painter.drawText(QPoint(tail_x, baseline_y), tail_seg)
                painter.setPen(underline_pen)
                underline_y = min(h - 3, baseline_y + 3)
                painter.drawLine(tail_x, underline_y, tail_x + t_w, underline_y)

        painter.end()
