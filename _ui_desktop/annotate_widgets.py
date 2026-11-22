"""截屏标注浮窗纯 UI 子控件（UI 验收标准 docs/design/v3.5/annotate-icons-b.html）。

单一职责：提供防穿透基类 `_OverlayBarFrame`、纯矢量图标主工具条 `AnnotateToolbar`、
按需纯圆点二级样式条 `AnnotateStyleBar`（6 色自绘色点 + 3 档纯圆点粗细 + 马赛克藏色）、
画布内联文字输入框 `InlineTextEdit` 与单源无修饰回车判定 `is_plain_enter_event`。
所有颜色、尺寸、圆角均取自 `_ui_desktop.theme`，不含业务流或 IO。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QEnterEvent,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from _ui_desktop import theme
from _ui_desktop.annotate_icons import ANNOTATE_ICON_KEYS, draw_annotate_icon
from _ui_desktop.annotate_tools import TOOL_MOSAIC, TOOL_TEXT, ToolName
from _ui_desktop.mini_menu import make_bar_divider

IconButtonRole = Literal["default", "primary", "danger"]
_VALID_ICON_ROLES: tuple[IconButtonRole, ...] = ("default", "primary", "danger")

_UNDO_TIP = "撤销"
_COPY_TIP = "复制 (Enter)"
_SAVE_TIP = "保存 PNG 到图片目录"
_CANCEL_TIP = "取消 (Esc)"

_DIVIDER_SLOT_W_PX = 7
_DIVIDER_SLOT_H_PX = 16
_DIVIDER_PAD_X_PX = 3

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


def _make_divider_slot(parent: QWidget) -> QWidget:
    """构建 7×16 分隔槽（左右 3px 留白 + 居中 1×16 分隔线，对齐模板 B `.bar .div`）。"""
    slot = QWidget(parent)
    slot.setFixedSize(_DIVIDER_SLOT_W_PX, _DIVIDER_SLOT_H_PX)
    box = QHBoxLayout(slot)
    box.setContentsMargins(_DIVIDER_PAD_X_PX, 0, _DIVIDER_PAD_X_PX, 0)
    box.setSpacing(0)
    box.addWidget(make_bar_divider(slot))
    return slot


def _size_tooltip(spec: theme.AnnotateSizeSpec, tool: ToolName | None) -> str:
    """按当前工具生成 3 档圆点的中文悬停与无障碍提示。"""
    if tool == TOOL_MOSAIC:
        return f"{spec.label} · 块尺寸 ({spec.block_px}px)"
    if tool == TOOL_TEXT:
        return f"{spec.label} · 字号 ({spec.text_px}px)"
    return f"{spec.label} ({spec.stroke_px}px)"


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


def _paint_rounded_box(
    painter: QPainter,
    rect: QRectF,
    bg_spec: theme.ColorSpec | None,
    border_spec: theme.ColorSpec | None,
) -> None:
    if bg_spec is None and border_spec is None:
        return
    inset = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    painter.setBrush(theme.qcolor(bg_spec) if bg_spec is not None else Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(theme.qcolor(border_spec), 1.0) if border_spec is not None else Qt.PenStyle.NoPen)
    painter.drawRoundedRect(inset, theme.RADIUS_SM, theme.RADIUS_SM)


class _HoverButton(QPushButton):
    """悬浮条自绘无焦点按钮基类：统一追踪悬停态并在禁用时清悬停与切换禁用光标。"""

    def __init__(self, obj_name: str, width: int, height: int, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._hovered = False
        self.setObjectName(obj_name)
        self.setFixedSize(width, height)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_tip(self, tip: str) -> None:
        self.setToolTip(tip)
        self.setAccessibleName(tip)

    def enterEvent(self, event: QEnterEvent) -> None:
        if self.isEnabled():
            self._hovered = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._hovered:
            self._hovered = False
            self.update()
        super().leaveEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._hovered = False
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self.isEnabled()
                else Qt.CursorShape.ForbiddenCursor
            )
            self.update()
        super().changeEvent(event)


class _IconButton(_HoverButton):
    """主工具条自绘纯矢量图标按钮：严格按 disabled > failed > active > role+hovered 优先级绘制。"""

    def __init__(
        self,
        icon_key: str,
        tooltip: str,
        *,
        role: IconButtonRole = "default",
        parent: QWidget | None = None,
    ) -> None:
        if icon_key not in ANNOTATE_ICON_KEYS:
            raise ValueError(f"不支持的标注图标键：{icon_key!r}，允许 {sorted(ANNOTATE_ICON_KEYS)}")
        if role not in _VALID_ICON_ROLES:
            raise ValueError(f"不支持的图标按钮语义角色：{role!r}，允许 {_VALID_ICON_ROLES}")
        btn_w = theme.ANNOTATE_PRIMARY_BTN_W_PX if role == "primary" else theme.ANNOTATE_ICON_BTN_PX
        super().__init__("oviIconBtn", btn_w, theme.ANNOTATE_ICON_BTN_PX, parent)
        self.icon_key = icon_key
        self.role: IconButtonRole = role
        self._active = False
        self._failed = False
        self.set_tip(tooltip)

    @property
    def is_failed(self) -> bool:
        return self._failed

    def set_active(self, active: bool) -> None:
        flag = bool(active)
        if self._active == flag:
            return
        self._active = flag
        self.setProperty("active", "true" if flag else "")
        self.update()

    def set_failed(self, failed: bool) -> None:
        flag = bool(failed)
        if self._failed == flag:
            return
        self._failed = flag
        self.setProperty("feedback", "fail" if flag else "")
        self.update()

    def _resolve_visual(
        self,
    ) -> tuple[theme.ColorSpec | None, theme.ColorSpec | None, theme.ColorSpec, float]:
        if not self.isEnabled():
            return None, None, theme.TEXT_MUTED_HEX, theme.ANNOTATE_DISABLED_OPACITY
        if self._failed:
            return theme.ERROR_BG_RGBA, theme.ERROR_BORDER_RGBA, theme.ERROR_HEX, 1.0
        if self._active:
            return theme.ACCENT_SOFT_RGBA, theme.ACCENT_ACTIVE_BORDER_RGBA, theme.ACCENT_HEX, 1.0
        if self.role == "primary":
            if self._hovered:
                return theme.OK_HOVER_BG_RGBA, theme.OK_HEX, theme.OK_BRIGHT_HEX, 1.0
            return theme.OK_SOFT_RGBA, theme.OK_BORDER_RGBA, theme.OK_HEX, 1.0
        if self.role == "danger":
            if self._hovered:
                return theme.ERROR_BG_RGBA, theme.ERROR_BORDER_RGBA, theme.ERROR_HEX, 1.0
            return None, None, theme.TEXT_SECONDARY_HEX, 1.0
        if self._hovered:
            return theme.ACCENT_SWATCH_HOVER_BG_RGBA, theme.ACCENT_HEX, theme.ACCENT_HEX, 1.0
        return None, None, theme.TEXT_PRIMARY_HEX, 1.0

    def _paint_content(
        self,
        painter: QPainter,
        bg_spec: theme.ColorSpec | None,
        border_spec: theme.ColorSpec | None,
        icon_spec: theme.ColorSpec,
    ) -> None:
        _paint_rounded_box(painter, QRectF(self.rect()), bg_spec, border_spec)
        glyph_px = float(theme.ANNOTATE_ICON_GLYPH_PX)
        painter.save()
        painter.translate((self.width() - glyph_px) / 2.0, (self.height() - glyph_px) / 2.0)
        draw_annotate_icon(painter, self.icon_key, theme.qcolor(icon_spec, opaque=True), size=glyph_px)
        painter.restore()

    def paintEvent(self, _event: QPaintEvent) -> None:
        bg_spec, border_spec, icon_spec, opacity = self._resolve_visual()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if opacity < 1.0:
            # 离屏整层合成后再按不透明度绘制，避免多笔画交点（如撤销箭头）重复叠混
            dpr = max(1.0, float(self.devicePixelRatioF()))
            layer = QImage(
                max(1, round(self.width() * dpr)),
                max(1, round(self.height() * dpr)),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            layer.setDevicePixelRatio(dpr)
            layer.fill(Qt.GlobalColor.transparent)
            lp = QPainter(layer)
            lp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_content(lp, bg_spec, border_spec, icon_spec)
            lp.end()
            painter.setOpacity(opacity)
            painter.drawImage(0, 0, layer)
        else:
            self._paint_content(painter, bg_spec, border_spec, icon_spec)
        painter.end()


class AnnotateToolbar(_OverlayBarFrame):
    """截屏标注效率语义图标主条（模板 B · 309×38px）：5工具 │ 撤销 │ 复制(主)·保存 │ 取消 [状态]。"""

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
        layout.setSpacing(2)

        self._tool_buttons: dict[str, _IconButton] = {}
        self._all_buttons: dict[str, _IconButton] = {}

        def _add_btn(
            key: str,
            tip: str,
            handler: Callable[[], None],
            *,
            role: IconButtonRole = "default",
            enabled: bool = True,
        ) -> _IconButton:
            btn = _IconButton(key, tip, role=role, parent=self)
            btn.setEnabled(enabled)
            btn.clicked.connect(lambda _checked=False, fn=handler: fn())
            layout.addWidget(btn)
            self._all_buttons[key] = btn
            return btn

        for key, label in tool_items:
            self._tool_buttons[key] = _add_btn(key, label, lambda k=key: on_pick_tool(k))

        layout.addWidget(_make_divider_slot(self))
        self._undo_btn = _add_btn("undo", _UNDO_TIP, on_undo, enabled=False)
        layout.addWidget(_make_divider_slot(self))
        self._copy_btn = _add_btn("copy", _COPY_TIP, on_copy, role="primary")
        self._save_btn = _add_btn("save", _SAVE_TIP, on_save)
        layout.addWidget(_make_divider_slot(self))
        self._cancel_btn = _add_btn("cancel", _CANCEL_TIP, on_cancel, role="danger")

        self._status = QLabel("", self)
        self._status.setObjectName("oviFloatOk")
        self._status.setContentsMargins(4, 0, 6, 0)
        self._status.hide()
        layout.addWidget(self._status)

    def button(self, key: str) -> QPushButton:
        """按工具名或动作名（rect/arrow/brush/text/mosaic/undo/copy/save/cancel）取按钮。"""
        return self._all_buttons[key]

    def visible_button_keys(self) -> list[str]:
        """当前可见按钮从左到右的键名列表。"""
        return [key for key, btn in self._all_buttons.items() if not btn.isHidden()]

    def is_copy_failed(self) -> bool:
        """复制主钮当前是否处于红框失败态。"""
        return self._copy_btn.is_failed

    def status_text(self) -> str:
        return "" if self._status.isHidden() else self._status.text()

    def set_active_tool(self, tool: str | None) -> None:
        for key, btn in self._tool_buttons.items():
            btn.set_active(key == tool)

    def set_undo_enabled(self, enabled: bool) -> None:
        self._undo_btn.setEnabled(bool(enabled))

    def set_copy_failed(self, failed: bool) -> None:
        """仅切换复制主钮红框失败态（锁宽 32px 不改文字），不触碰右侧状态栏。"""
        self._copy_btn.set_failed(failed)

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


class _SwatchButton(_HoverButton):
    """24×24 自绘色点按钮：深色底高可见轮廓 + 选中态「2px 蓝环 + 4px 反色中心点」双编码。"""

    def __init__(self, spec: theme.AnnotateColorSpec, parent: QWidget | None = None) -> None:
        super().__init__("oviSwatchBtn", theme.ANNOTATE_SWATCH_BTN_PX, theme.ANNOTATE_SWATCH_BTN_PX, parent)
        self.spec = spec
        self._selected = False
        self.set_tip(f"{spec.label} ({spec.hex})")

    def set_selected(self, selected: bool) -> None:
        flag = bool(selected)
        if self._selected == flag:
            return
        self._selected = flag
        self.setProperty("selected", "true" if flag else "")
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        center = rect.center()

        if self._selected or self._hovered:
            outer = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            bg = theme.ACCENT_SOFT_RGBA if self._selected else theme.ACCENT_SWATCH_HOVER_BG_RGBA
            border = theme.ACCENT_HEX if self._selected else theme.ACCENT_SWATCH_HOVER_BORDER_RGBA
            painter.setBrush(theme.qcolor(bg))
            painter.setPen(QPen(theme.qcolor(border, opaque=self._selected), 2.0))
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


class _SizeButton(_HoverButton):
    """24×24 纯圆点粗细按钮（直径 4/7/10px，无文字，动态 ToolTip，保留 active 属性）。"""

    def __init__(self, spec: theme.AnnotateSizeSpec, parent: QWidget | None = None) -> None:
        super().__init__("oviSizeBtn", theme.ANNOTATE_SIZE_BTN_PX, theme.ANNOTATE_SIZE_BTN_PX, parent)
        self.spec = spec
        self._selected = False
        self.update_tooltip(None)

    def update_tooltip(self, tool: ToolName | None) -> None:
        self.set_tip(_size_tooltip(self.spec, tool))

    def set_selected(self, selected: bool) -> None:
        flag = bool(selected)
        if self._selected == flag:
            return
        self._selected = flag
        self.setProperty("active", "true" if flag else "")
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._selected:
            _paint_rounded_box(painter, QRectF(self.rect()), theme.ACCENT_SOFT_RGBA, theme.ACCENT_ACTIVE_BORDER_RGBA)
        elif self._hovered:
            _paint_rounded_box(painter, QRectF(self.rect()), theme.ACCENT_SWATCH_HOVER_BG_RGBA, theme.ACCENT_HEX)

        dot_color = theme.ACCENT_HEX if (self._selected or self._hovered) else theme.TEXT_SECONDARY_HEX
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(dot_color, opaque=True))
        r = self.spec.dot_px / 2.0
        painter.drawEllipse(QRectF(self.rect()).center(), r, r)
        painter.end()


class AnnotateStyleBar(_OverlayBarFrame):
    """模板 B 按需二级样式条（锁高 34px）：6 色色点 │ 3 档纯圆点 + 等宽 px 数值；马赛克隐藏色板。"""

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

        self._divider = _make_divider_slot(self)
        layout.addWidget(self._divider)

        self._size_box = QWidget(self)
        sz_layout = QHBoxLayout(self._size_box)
        sz_layout.setContentsMargins(0, 0, 0, 0)
        sz_layout.setSpacing(2)
        self._size_buttons: dict[str, _SizeButton] = {}
        for sz in theme.ANNOTATE_SIZES:
            btn = _SizeButton(sz, self._size_box)
            btn.clicked.connect(lambda _checked=False, key=sz.key: on_pick_size(key))
            sz_layout.addWidget(btn)
            self._size_buttons[sz.key] = btn
        layout.addWidget(self._size_box)

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

    def set_state(self, *, tool: ToolName | None, color: str, size_level: str) -> None:
        validated_color, size_spec = theme.resolve_annotate_style(color, size_level)
        for hx, btn in self._swatch_buttons.items():
            btn.set_selected(hx == validated_color)
        for key, btn in self._size_buttons.items():
            btn.set_selected(key == size_spec.key)
            btn.update_tooltip(tool)
        if tool is None:
            self.hide()
            return
        is_mosaic = tool == TOOL_MOSAIC
        self._swatch_box.setVisible(not is_mosaic)
        self._divider.setVisible(not is_mosaic)
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
