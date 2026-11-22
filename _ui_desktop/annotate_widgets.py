"""截屏标注浮窗纯 UI 子控件（UI 验收标准 docs/design/v3.5/annotate-brush-b.html）。

单一职责：提供防穿透基类 `_OverlayBarFrame`、主工具条 `AnnotateToolbar`、
按需二级样式条 `AnnotateStyleBar`（6 色自绘色点 + 3 档圆点粗细 + 马赛克藏色）、
画布内联文字输入框 `InlineTextEdit` 与单源无修饰回车判定 `is_plain_enter_event`。
所有颜色、尺寸、圆角均取自 `_ui_desktop.theme`，不含业务流或 IO。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QEnterEvent,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from _ui_desktop import theme
from _ui_desktop.annotate_tools import TOOL_MOSAIC, TOOL_TEXT, ToolName
from _ui_desktop.mini_menu import (
    COPY_FAILED_TEXT,
    make_bar_button,
    make_bar_divider,
    set_button_property,
)

UNDO_TEXT = "撤销"
COPY_TEXT = "复制"
SAVE_TEXT = "保存"
CANCEL_TEXT = "取消"
ANNOTATE_KEY_HINT = "Enter/Esc"
MOSAIC_STYLE_LABEL = "马赛克块："

_DISALLOWED_ENTER_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.ShiftModifier
    | Qt.KeyboardModifier.MetaModifier
)


def is_plain_enter_event(event: QKeyEvent) -> bool:
    """判定是否为主键盘或小键盘的无修饰 Enter（放行 KeypadModifier，屏蔽 Ctrl/Alt/Shift/Meta）。"""
    if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return False
    return not bool(event.modifiers() & _DISALLOWED_ENTER_MODIFIERS)


class _OverlayBarFrame(QFrame):
    """浮窗内悬浮条基类：箭头光标 + 吞掉鼠标按下/移动/松开，阻止 inside 模式下事件冒泡到画布。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()


class AnnotateToolbar(_OverlayBarFrame):
    """截屏标注精简主条（模板 B）：矩形·箭头·画笔·文字·马赛克 │ 撤销 │ 复制·保存 │ 取消 Enter/Esc [状态]。"""

    def __init__(
        self,
        *,
        tool_items: Sequence[tuple[str, str]],
        on_pick_tool: Callable[[str], None],
        on_undo: Callable[[], None],
        on_copy: Callable[[], None],
        on_save: Callable[[], None],
        on_cancel: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("oviFloatBar")
        self.setStyleSheet(theme.float_bar_stylesheet())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self._tool_buttons: dict[str, QPushButton] = {}
        self._all_buttons: dict[str, QPushButton] = {}
        for key, label in tool_items:
            btn = make_bar_button(self, label)
            btn.clicked.connect(lambda _checked=False, k=key: on_pick_tool(k))
            layout.addWidget(btn)
            self._tool_buttons[key] = btn
            self._all_buttons[key] = btn

        layout.addWidget(make_bar_divider(self))
        self._undo_btn = make_bar_button(self, UNDO_TEXT)
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(on_undo)
        layout.addWidget(self._undo_btn)
        self._all_buttons["undo"] = self._undo_btn

        layout.addWidget(make_bar_divider(self))
        self._copy_btn = make_bar_button(self, COPY_TEXT)
        self._copy_btn.clicked.connect(on_copy)
        layout.addWidget(self._copy_btn)
        self._all_buttons["copy"] = self._copy_btn

        self._save_btn = make_bar_button(self, SAVE_TEXT)
        self._save_btn.clicked.connect(on_save)
        layout.addWidget(self._save_btn)
        self._all_buttons["save"] = self._save_btn

        layout.addWidget(make_bar_divider(self))
        self._cancel_btn = make_bar_button(self, CANCEL_TEXT)
        self._cancel_btn.clicked.connect(on_cancel)
        layout.addWidget(self._cancel_btn)
        self._all_buttons["cancel"] = self._cancel_btn

        hint = QLabel(ANNOTATE_KEY_HINT, self)
        hint.setObjectName("oviFloatHint")
        hint.setFont(theme.make_font(pixel_size=theme.FS_MONO_TAG, mono=True))
        hint.setContentsMargins(4, 0, 6, 0)
        layout.addWidget(hint)
        self._key_hint = hint

        self._status = QLabel("", self)
        self._status.setObjectName("oviFloatOk")
        self._status.setContentsMargins(6, 0, 8, 0)
        self._status.hide()
        layout.addWidget(self._status)

    def button(self, key: str) -> QPushButton:
        """按工具名或动作名（rect/arrow/brush/text/mosaic/undo/copy/save/cancel）取按钮。"""
        return self._all_buttons[key]

    def visible_labels(self) -> list[str]:
        """当前可见按钮的从左到右文案列表。"""
        return [btn.text() for btn in self._all_buttons.values() if not btn.isHidden()]

    def key_hint_text(self) -> str:
        return self._key_hint.text()

    def status_text(self) -> str:
        return "" if self._status.isHidden() else self._status.text()

    def set_active_tool(self, tool: str | None) -> None:
        for key, btn in self._tool_buttons.items():
            set_button_property(btn, "active", "true" if key == tool else "")

    def set_undo_enabled(self, enabled: bool) -> None:
        self._undo_btn.setEnabled(bool(enabled))

    def set_copy_failed(self, failed: bool) -> None:
        self._copy_btn.setText(COPY_FAILED_TEXT if failed else COPY_TEXT)
        set_button_property(self._copy_btn, "feedback", "fail" if failed else "")
        self.adjustSize()

    def set_status(self, text: str, *, ok: bool) -> None:
        if not text:
            self._status.clear()
            self._status.hide()
        else:
            self._status.setObjectName("oviFloatOk" if ok else "oviFloatError")
            self._status.setText(text)
            self._status.show()
            self.setStyleSheet(theme.float_bar_stylesheet())
        self.adjustSize()


class _SwatchButton(QPushButton):
    """24×24 自绘色点按钮：深色底高可见轮廓 + 选中态「2px 蓝环 + 4px 反色中心点」双编码。"""

    def __init__(self, spec: theme.AnnotateColorSpec, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.spec = spec
        self._selected = False
        self._hovered = False
        self.setObjectName("oviSwatchBtn")
        self.setFixedSize(theme.ANNOTATE_SWATCH_BTN_PX, theme.ANNOTATE_SWATCH_BTN_PX)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{spec.label} ({spec.hex})")

    def set_selected(self, selected: bool) -> None:
        flag = bool(selected)
        if self._selected == flag:
            return
        self._selected = flag
        self.setProperty("selected", "true" if flag else "")
        self.update()

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        center = rect.center()

        if self._selected:
            outer = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            painter.setBrush(theme.qcolor(theme.ACCENT_SOFT_RGBA))
            painter.setPen(QPen(theme.qcolor(theme.ACCENT_HEX, opaque=True), 2.0))
            painter.drawEllipse(outer)
        elif self._hovered:
            outer = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            painter.setBrush(theme.qcolor(theme.ACCENT_SWATCH_HOVER_BG_RGBA))
            painter.setPen(QPen(theme.qcolor(theme.ACCENT_SWATCH_HOVER_BORDER_RGBA), 2.0))
            painter.drawEllipse(outer)

        r = theme.ANNOTATE_SWATCH_CIRCLE_PX / 2.0
        painter.setBrush(theme.qcolor(self.spec.hex, opaque=True))
        painter.setPen(QPen(theme.qcolor(self.spec.ring_rgba), 1.0))
        painter.drawEllipse(center, r, r)

        if self._selected:
            dr = theme.ANNOTATE_SWATCH_DOT_PX / 2.0
            painter.setBrush(theme.qcolor(self.spec.dot_hex, opaque=True))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, dr, dr)
        painter.end()


class _SizeButton(QPushButton):
    """46×24 自绘粗细按钮：圆点直径（4/7/10px）+ 细/中/粗 双编码，选中不改字重防抖动。"""

    def __init__(self, spec: theme.AnnotateSizeSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec.label, parent)
        self.spec = spec
        self._selected = False
        self._hovered = False
        self.setObjectName("oviSizeBtn")
        self.setFixedSize(theme.ANNOTATE_SIZE_BTN_W_PX, theme.ANNOTATE_SIZE_BTN_H_PX)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{spec.label}（线 {spec.stroke_px}px / 字 {spec.text_px}px / 马赛克 {spec.block_px}px）")

    def set_selected(self, selected: bool) -> None:
        flag = bool(selected)
        if self._selected == flag:
            return
        self._selected = flag
        self.setProperty("active", "true" if flag else "")
        self.update()

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._selected:
            painter.setBrush(theme.qcolor(theme.ACCENT_SOFT_RGBA))
            painter.setPen(QPen(theme.qcolor(theme.ACCENT_ACTIVE_BORDER_RGBA), 1.0))
            painter.drawRoundedRect(rect, theme.RADIUS_SM, theme.RADIUS_SM)
        elif self._hovered:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT_HEX, opaque=True), 1.0))
            painter.drawRoundedRect(rect, theme.RADIUS_SM, theme.RADIUS_SM)

        font = theme.make_font(pixel_size=theme.FS_XS, mono=False, weight=QFont.Weight.Medium)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text_w = fm.horizontalAdvance(self.spec.label)
        gap = 5.0
        total_w = self.spec.dot_px + gap + text_w
        left = (self.width() - total_w) / 2.0
        cy = self.height() / 2.0

        dot_color = theme.ACCENT_HEX if self._selected else theme.TEXT_SECONDARY_HEX
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(dot_color, opaque=True))
        r = self.spec.dot_px / 2.0
        painter.drawEllipse(QPointF(left + r, cy), r, r)

        text_color = theme.ACCENT_HEX if (self._selected or self._hovered) else theme.TEXT_PRIMARY_HEX
        painter.setPen(theme.qcolor(text_color, opaque=True))
        text_rect = QRectF(left + self.spec.dot_px + gap, 0.0, text_w + 2.0, float(self.height()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.spec.label)
        painter.end()


class AnnotateStyleBar(_OverlayBarFrame):
    """模板 B 按需二级样式条：6 色色点 │ 3 档粗细 + 等宽 px 徽标；马赛克隐藏色板显「马赛克块：」。"""

    def __init__(
        self,
        *,
        on_pick_color: Callable[[str], None],
        on_pick_size: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("oviStyleBar")
        self.setStyleSheet(theme.float_bar_stylesheet())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)

        self._swatch_box = QWidget(self)
        sw_layout = QHBoxLayout(self._swatch_box)
        sw_layout.setContentsMargins(0, 0, 0, 0)
        sw_layout.setSpacing(3)
        self._swatch_buttons: dict[str, _SwatchButton] = {}
        for spec in theme.ANNOTATE_COLORS:
            btn = _SwatchButton(spec, self._swatch_box)
            btn.clicked.connect(lambda _checked=False, hx=spec.hex: on_pick_color(hx))
            sw_layout.addWidget(btn)
            self._swatch_buttons[spec.hex] = btn
        layout.addWidget(self._swatch_box)

        self._divider = make_bar_divider(self)
        layout.addWidget(self._divider)

        self._mosaic_label = QLabel(MOSAIC_STYLE_LABEL, self)
        self._mosaic_label.setObjectName("oviStyleHint")
        self._mosaic_label.setContentsMargins(2, 0, 4, 0)
        self._mosaic_label.hide()
        layout.addWidget(self._mosaic_label)

        self._size_buttons: dict[str, _SizeButton] = {}
        for sz in theme.ANNOTATE_SIZES:
            btn = _SizeButton(sz, self)
            btn.clicked.connect(lambda _checked=False, key=sz.key: on_pick_size(key))
            layout.addWidget(btn)
            self._size_buttons[sz.key] = btn

        self._px_label = QLabel("", self)
        self._px_label.setObjectName("oviStylePx")
        self._px_label.setFont(theme.make_font(pixel_size=theme.FS_MONO_TAG, mono=True))
        self._px_label.setMinimumWidth(theme.ANNOTATE_PX_TAG_MIN_W_PX)
        self._px_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._px_label)

        # 完整态一次量高并锁死高度，确保色板态与马赛克态等高零纵向抖动
        self.adjustSize()
        self._reserved_h = max(theme.ANNOTATE_SUB_BAR_RESERVE_H_PX, self.sizeHint().height())
        self.setFixedHeight(self._reserved_h)
        self.hide()

    def reserved_height(self) -> int:
        """二级样式条锁定高度（供主条按联合高度一次性锁锚）。"""
        return self._reserved_h

    def swatch_button(self, color_hex: str) -> QPushButton:
        return self._swatch_buttons[theme.validate_annotate_color(color_hex)]

    def size_button(self, size_level: str) -> QPushButton:
        return self._size_buttons[theme.validate_annotate_size(size_level).key]

    def px_text(self) -> str:
        return self._px_label.text()

    def is_palette_visible(self) -> bool:
        return not self._swatch_box.isHidden()

    def is_mosaic_label_visible(self) -> bool:
        return not self._mosaic_label.isHidden()

    def set_state(self, *, tool: ToolName | None, color: str, size_level: str) -> None:
        validated_color, size_spec = theme.resolve_annotate_style(color, size_level)
        for hx, btn in self._swatch_buttons.items():
            btn.set_selected(hx == validated_color)
        for key, btn in self._size_buttons.items():
            btn.set_selected(key == size_spec.key)
        if tool is None:
            self.hide()
            return
        is_mosaic = tool == TOOL_MOSAIC
        self._swatch_box.setVisible(not is_mosaic)
        self._divider.setVisible(not is_mosaic)
        self._mosaic_label.setVisible(is_mosaic)
        px = size_spec.block_px if is_mosaic else (size_spec.text_px if tool == TOOL_TEXT else size_spec.stroke_px)
        self._px_label.setText(f"{px}px")
        self.adjustSize()
        self.show()


class InlineTextEdit(QLineEdit):
    """画布内联文字输入框：开启输入法支持中文，无修饰 Enter 提交、修饰 Enter 吞掉不冒泡、Esc 仅取消本次输入。"""

    def __init__(
        self,
        *,
        color: str,
        text_px: int,
        on_commit: Callable[[], None],
        on_cancel: Callable[[], None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self._text_px = text_px
        self.setObjectName("oviAnnotateEdit")
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.apply_style(color=color, text_px=text_px)

    def apply_style(self, *, color: str, text_px: int) -> None:
        """实时更新内联输入框字体像素尺寸与字色（文字输入中切色/切粗细立即可见）。"""
        self._text_px = max(1, int(text_px))
        self.setFont(theme.make_font(pixel_size=self._text_px, mono=False, weight=QFont.Weight.DemiBold))
        self.setStyleSheet(theme.annotate_edit_stylesheet(color=color))

    def fit_in_canvas(self, canvas: QRect, anchor: QPointF) -> QPointF:
        """按当前内容与字号自适应宽高并重钳制到选区画布内，返回更新后的逻辑锚点。"""
        h = self._text_px + theme.ANNOTATE_EDIT_EXTRA_HEIGHT_PX
        min_w = min(theme.ANNOTATE_EDIT_MIN_WIDTH_PX, max(1, canvas.width()))
        max_x = max(canvas.x(), canvas.x() + canvas.width() - min_w)
        max_y = max(canvas.y(), canvas.y() + canvas.height() - h)
        x = max(canvas.x(), min(canvas.x() + round(anchor.x()), max_x))
        y = max(canvas.y(), min(canvas.y() + round(anchor.y()) - 2, max_y))
        fm = QFontMetrics(self.font())
        max_w = max(min_w, canvas.x() + canvas.width() - x)
        w = min(
            max_w,
            max(min_w, fm.horizontalAdvance(self.text()) + theme.ANNOTATE_EDIT_PADDING_X_PX),
        )
        self.setGeometry(x, y, w, h)
        return QPointF(float(x - canvas.x()), float(y + 2 - canvas.y()))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            if is_plain_enter_event(event):
                self._on_commit()
            return
        if key == Qt.Key.Key_Escape:
            event.accept()
            self._on_cancel()
            return
        super().keyPressEvent(event)
