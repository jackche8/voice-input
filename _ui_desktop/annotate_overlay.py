"""截屏标注浮窗（UI 验收标准 docs/design/v3.4/annotate-a.html「一条迷你工具条」）。

单一职责：全屏透明窗口，在选区原位显示冻结截图与标注合成画布，选区外覆暗色遮罩，
工具条复用 `mini_menu.AnnotateToolbar` 与 `place_menu` 避让定位；点框外不关闭；
复制成功立即关闭、失败提示 1.5s 不关；保存成功显示路径 `theme.ANNOTATE_SAVED_FEEDBACK_MS`
后关闭、失败显示原因不关；Esc 按焦点状态裁决（文字输入中仅结束输入，非输入态关闭）。
纯 UI，不含文件 IO、网络或剪贴板调用。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, cast

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QLineEdit, QWidget

from _ui_desktop import theme
from _ui_desktop.annotate_tools import (
    TOOL_ITEMS,
    TOOL_TEXT,
    VALID_TOOLS,
    AnnotationOp,
    ToolName,
    apply_op_to_image,
    clamp_point,
    encode_png,
    make_op,
    normalized_rect,
    paint_draft_op,
    render_composite,
)
from _ui_desktop.mini_menu import AnnotateToolbar, has_window_keyboard_focus, place_menu

SAVED_PREFIX = "✓ 已保存 "
SAVE_FAILED_PREFIX = "保存失败："


class SaveResultLike(Protocol):
    """业务层保存回调返回协议（对齐 `_desktop.capture_flow.SaveResult`）。"""

    @property
    def path(self) -> str | None: ...

    @property
    def error(self) -> str | None: ...


class _InlineTextEdit(QLineEdit):
    """画布内联文字输入框：开启输入法支持中文，Enter 提交、Esc 仅取消本次输入。"""

    def __init__(
        self,
        *,
        on_commit: Callable[[], None],
        on_cancel: Callable[[], None],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self.setObjectName("oviAnnotateEdit")
        self.setStyleSheet(theme.annotate_edit_stylesheet(color=theme.CROSSHAIR_HEX))
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self._on_commit()
            return
        if key == Qt.Key.Key_Escape:
            event.accept()
            self._on_cancel()
            return
        super().keyPressEvent(event)


class AnnotateOverlay(QWidget):
    """全屏透明截屏标注浮窗：冻结图 + 标注画布 + 框下/框上/框内迷你工具条。"""

    def __init__(
        self,
        cropped_png: bytes,
        screen_rect: tuple[int, int, int, int],
        *,
        virtual_geometry: tuple[int, int, int, int] | None = None,
        on_copy_image: Callable[[bytes], bool] | None = None,
        on_save_image: Callable[[bytes], SaveResultLike] | None = None,
        on_closed: Callable[[], None] | None = None,
        available_screen_rect: tuple[int, int, int, int] | None = None,
        copy_feedback_ms: int = theme.ANNOTATE_COPY_FEEDBACK_MS,
        saved_feedback_ms: int = theme.ANNOTATE_SAVED_FEEDBACK_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not cropped_png:
            raise ValueError("cropped_png 不能为空")
        base = QImage.fromData(cropped_png)
        if base.isNull() or base.width() <= 0 or base.height() <= 0:
            raise ValueError("cropped_png 不是有效的图像数据")
        sx, sy, sw, sh = (int(v) for v in screen_rect)
        if sw <= 0 or sh <= 0:
            raise ValueError(f"screen_rect 宽高必须大于 0，收到 {screen_rect!r}")
        vx, vy, vw, vh = theme.resolve_virtual_geometry(virtual_geometry)

        self._base = base.convertToFormat(QImage.Format.Format_ARGB32)
        self._screen_rect = QRect(sx, sy, sw, sh)
        self._origin = QPoint(vx, vy)
        self._canvas = QRect(sx - vx, sy - vy, sw, sh)
        self._avail = theme.resolve_available_screen_rect(available_screen_rect, QPoint(sx, sy))

        self._on_copy_image = on_copy_image
        self._on_save_image = on_save_image
        self._on_closed = on_closed
        self._copy_feedback_ms = max(1, int(copy_feedback_ms))
        self._saved_feedback_ms = max(1, int(saved_feedback_ms))
        self._closed_notified = False

        self._tool: ToolName | None = None
        self._ops: list[AnnotationOp] = []
        self._composite: QImage = self._base.copy()
        self._draft: AnnotationOp | None = None
        self._text_edit: _InlineTextEdit | None = None
        self._text_anchor: QPointF | None = None
        self._text_font: QFont = theme.make_font(
            pixel_size=theme.ANNOTATE_TEXT_PX,
            mono=False,
            weight=QFont.Weight.DemiBold,
        )
        self._menu_placement = "below"

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setGeometry(vx, vy, vw, vh)

        self._copy_reset_timer = QTimer(self)
        self._copy_reset_timer.setSingleShot(True)
        self._copy_reset_timer.timeout.connect(self._clear_copy_failed)

        self._save_close_timer = QTimer(self)
        self._save_close_timer.setSingleShot(True)
        self._save_close_timer.timeout.connect(self.close)

        self.toolbar = AnnotateToolbar(
            tool_items=TOOL_ITEMS,
            on_pick_tool=self.pick_tool,
            on_undo=self.undo,
            on_copy=self._copy_image,
            on_save=self._save_image,
            on_cancel=self._cancel_and_close,
            parent=self,
        )
        self._place_toolbar()

    # ------------------------------------------------------------------
    # 公共接口（capture_flow.AnnotatorHandle 与状态查询）
    # ------------------------------------------------------------------
    @property
    def menu_placement(self) -> str:
        """迷你工具条当前位置：below / above / inside。"""
        return self._menu_placement

    @property
    def current_tool(self) -> ToolName | None:
        return self._tool

    @property
    def is_editing_text(self) -> bool:
        return self._text_edit is not None

    @property
    def operations(self) -> tuple[AnnotationOp, ...]:
        return tuple(self._ops)

    def canvas_rect(self) -> QRect:
        """选区画布在本窗内的逻辑矩形。"""
        return QRect(self._canvas)

    def export_image(self) -> QImage:
        """返回与原冻结图同分辨率的当前合成图像副本。"""
        return self._composite.copy()

    def export_png(self) -> bytes:
        """将当前冻结图 + 全部已提交标注编码为原分辨率 PNG 字节流。"""
        return encode_png(self._composite)

    def pick_tool(self, tool: str) -> None:
        if tool not in VALID_TOOLS:
            raise ValueError(f"不支持的标注工具：{tool!r}，允许 {sorted(VALID_TOOLS)}")
        if self._text_edit is not None:
            self._commit_text()
        typed_tool = cast(ToolName, tool)
        self._tool = typed_tool
        self.toolbar.set_active_tool(typed_tool)

    def undo(self) -> None:
        if not self._ops:
            return
        self._ops.pop()
        self._composite = render_composite(self._base, self._ops, self._canvas.size())
        self.toolbar.set_undo_enabled(bool(self._ops))
        self.update()

    def handle_escape(self) -> None:
        """全局 Esc（按焦点状态裁决）：持有焦点时交窗内按键事件，无焦点时执行同款 Esc 处理。"""
        if has_window_keyboard_focus(self):
            return
        self._handle_escape_action()

    def close_silent(self) -> None:
        """关闭但不回调 on_closed（被新一次截屏取代或应用退出时）。"""
        self._closed_notified = True
        self._stop_timers()
        self._cancel_text()
        self.close()

    # ------------------------------------------------------------------
    # 工具条动作：复制 / 保存 / 取消 / Esc
    # ------------------------------------------------------------------
    def _handle_escape_action(self) -> None:
        if self._text_edit is not None:
            self._cancel_text()
            return
        self.close()

    def _cancel_and_close(self) -> None:
        self._cancel_text()
        self.close()

    def _copy_image(self) -> None:
        if self._text_edit is not None:
            self._commit_text()
        ok = bool(self._on_copy_image(self.export_png())) if self._on_copy_image is not None else False
        if ok:
            self._copy_reset_timer.stop()
            self.close()
            return
        self.toolbar.set_copy_failed(True)
        self._place_toolbar()
        self._copy_reset_timer.start(self._copy_feedback_ms)

    def _clear_copy_failed(self) -> None:
        self.toolbar.set_copy_failed(False)
        self._place_toolbar()

    def _save_image(self) -> None:
        if self._text_edit is not None:
            self._commit_text()
        if self._on_save_image is None:
            self._save_close_timer.stop()
            self.toolbar.set_status(f"{SAVE_FAILED_PREFIX}未配置保存回调", ok=False)
            self._place_toolbar()
            return
        result = self._on_save_image(self.export_png())
        if bool(result.path) == bool(result.error):
            raise ValueError(f"SaveResult 必须恰好包含 path 或 error 之一，收到 {result!r}")
        if result.path:
            self.toolbar.set_status(f"{SAVED_PREFIX}{result.path}", ok=True)
            self._place_toolbar()
            self._save_close_timer.start(self._saved_feedback_ms)
            return
        self._save_close_timer.stop()
        self.toolbar.set_status(f"{SAVE_FAILED_PREFIX}{result.error}", ok=False)
        self._place_toolbar()

    def _place_toolbar(self) -> None:
        self.toolbar.adjustSize()
        top_left, placement = place_menu(self._screen_rect, self.toolbar.size(), self._avail)
        self.toolbar.move(top_left - self._origin)
        self._menu_placement = placement

    def _stop_timers(self) -> None:
        self._copy_reset_timer.stop()
        self._save_close_timer.stop()

    # ------------------------------------------------------------------
    # 文字内联编辑与标注提交
    # ------------------------------------------------------------------
    def _open_text(self, canvas_pt: QPointF) -> None:
        self._cancel_text()
        edit = _InlineTextEdit(on_commit=self._commit_text, on_cancel=self._cancel_text, parent=self)
        edit.setFont(self._text_font)
        h = theme.ANNOTATE_TEXT_PX + theme.ANNOTATE_EDIT_EXTRA_HEIGHT_PX
        min_w = min(theme.ANNOTATE_EDIT_MIN_WIDTH_PX, max(1, self._canvas.width()))
        max_x = max(self._canvas.x(), self._canvas.x() + self._canvas.width() - min_w)
        max_y = max(self._canvas.y(), self._canvas.y() + self._canvas.height() - h)
        x = max(self._canvas.x(), min(self._canvas.x() + round(canvas_pt.x()), max_x))
        y = max(self._canvas.y(), min(self._canvas.y() + round(canvas_pt.y()) - 2, max_y))
        self._text_edit = edit
        self._text_anchor = QPointF(x - self._canvas.x(), y + 2 - self._canvas.y())
        edit.textChanged.connect(lambda _text: self._fit_text_edit())
        self._fit_text_edit()
        edit.show()
        edit.setFocus(Qt.FocusReason.MouseFocusReason)

    def _fit_text_edit(self) -> None:
        edit = self._text_edit
        anchor = self._text_anchor
        if edit is None or anchor is None:
            return
        x = self._canvas.x() + round(anchor.x())
        y = self._canvas.y() + round(anchor.y()) - 2
        h = theme.ANNOTATE_TEXT_PX + theme.ANNOTATE_EDIT_EXTRA_HEIGHT_PX
        fm = QFontMetrics(self._text_font)
        max_w = max(theme.ANNOTATE_EDIT_MIN_WIDTH_PX, self._canvas.x() + self._canvas.width() - x)
        w = min(
            max_w,
            max(theme.ANNOTATE_EDIT_MIN_WIDTH_PX, fm.horizontalAdvance(edit.text()) + theme.ANNOTATE_EDIT_PADDING_X_PX),
        )
        edit.setGeometry(x, y, w, h)

    def _detach_text_edit(self) -> tuple[str, QPointF | None]:
        edit = self._text_edit
        if edit is None:
            return "", None
        anchor = self._text_anchor
        text = edit.text().strip()
        self._text_edit = None
        self._text_anchor = None
        if self.isActiveWindow():
            self.setFocus()
        edit.hide()
        edit.deleteLater()
        return text, anchor

    def _commit_text(self) -> None:
        text, anchor = self._detach_text_edit()
        if text and anchor is not None:
            self._commit_op(make_op(TOOL_TEXT, anchor, anchor, text=text))

    def _cancel_text(self) -> None:
        self._detach_text_edit()

    def _commit_op(self, op: AnnotationOp) -> None:
        self._ops.append(op)
        apply_op_to_image(self._composite, op, self._canvas.size())
        self.toolbar.set_undo_enabled(True)
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        self._paint_dim_outside(painter)
        painter.drawImage(QRectF(self._canvas), self._composite)
        if self._draft is not None:
            paint_draft_op(painter, self._draft, self._canvas, self._composite)
        self._paint_outline(painter)
        painter.end()

    def _paint_dim_outside(self, painter: QPainter) -> None:
        w, h = self.width(), self.height()
        cx, cy, cw, ch = self._canvas.x(), self._canvas.y(), self._canvas.width(), self._canvas.height()
        dim = theme.qcolor(theme.DIM_MASK_RGBA)
        if cy > 0:
            painter.fillRect(QRect(0, 0, w, cy), dim)
        if cy + ch < h:
            painter.fillRect(QRect(0, cy + ch, w, h - (cy + ch)), dim)
        if cx > 0:
            painter.fillRect(QRect(0, cy, cx, ch), dim)
        if cx + cw < w:
            painter.fillRect(QRect(cx + cw, cy, w - (cx + cw), ch), dim)

    def _paint_outline(self, painter: QPainter) -> None:
        width = theme.OVERLAY_OUTLINE_WIDTH_PX
        pen = QPen(theme.qcolor(theme.OVERLAY_OUTLINE_HEX, opaque=True), width)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = width / 2.0
        painter.drawRect(QRectF(self._canvas).adjusted(-half, -half, half, half))

    # ------------------------------------------------------------------
    # 鼠标与键盘事件（点框外不关闭）
    # ------------------------------------------------------------------
    def _to_canvas(self, pos: QPointF) -> QPointF:
        return clamp_point(
            QPointF(pos.x() - self._canvas.x(), pos.y() - self._canvas.y()),
            self._canvas.width(),
            self._canvas.height(),
        )

    def _update_cursor(self, pos: QPoint) -> None:
        if not self._canvas.contains(pos) or self._tool is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._tool == TOOL_TEXT:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        pos = event.position().toPoint()
        if not self._canvas.contains(pos):
            if self._text_edit is not None:
                self._commit_text()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._text_edit is not None:
            self._commit_text()
            return
        if self._tool is None:
            return
        pt = self._to_canvas(event.position())
        if self._tool == TOOL_TEXT:
            self._open_text(pt)
            return
        self._draft = make_op(self._tool, pt, pt)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._update_cursor(event.position().toPoint())
        if self._draft is not None:
            self._draft = replace(self._draft, end=self._to_canvas(event.position()))
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._draft is None:
            event.accept()
            return
        draft = replace(self._draft, end=self._to_canvas(event.position()))
        self._draft = None
        rect = normalized_rect(draft.start, draft.end)
        if rect.width() + rect.height() > theme.ANNOTATE_MIN_DRAG_PX:
            self._commit_op(draft)
        else:
            self.update()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._handle_escape_action()
            return
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_timers()
        self._cancel_text()
        if not self._closed_notified:
            self._closed_notified = True
            if self._on_closed is not None:
                self._on_closed()
        super().closeEvent(event)
