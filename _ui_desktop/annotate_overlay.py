"""截屏标注浮窗（UI 验收标准 docs/design/v3.5/annotate-icons-b.html 模板 B）。

单一职责：全屏透明窗口，在选区原位显示冻结截图与标注合成画布，选区外覆暗色遮罩，
采用纯矢量图标主条 `AnnotateToolbar` + 按需纯圆点二级样式条 `AnnotateStyleBar`（6 色 + 3 档，
马赛克藏色，联合高度锁锚保证主条 0px 抖动），支持自由手绘画笔与无修饰 Enter 自动复制；
点框外不关闭；复制成功立即关闭、失败复制钮亮红框锁宽且右侧提示「复制失败」1.5s 不关；
保存成功显示路径 `theme.ANNOTATE_SAVED_FEEDBACK_MS` 后关闭、失败显示原因不关；
复制失败与保存反馈严格四态互斥；Esc 按焦点状态裁决。纯 UI，不含文件 IO、网络或剪贴板调用。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, cast

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from _ui_desktop import theme
from _ui_desktop.annotate_tools import (
    TOOL_BRUSH,
    TOOL_ITEMS,
    TOOL_TEXT,
    VALID_TOOLS,
    AnnotationOp,
    ToolName,
    append_brush_point,
    apply_op_to_image,
    clamp_point,
    encode_png,
    is_valid_op,
    make_op,
    paint_draft_op,
    render_composite,
)
from _ui_desktop.annotate_widgets import AnnotateStyleBar, AnnotateToolbar, InlineTextEdit, is_plain_enter_event
from _ui_desktop.mini_menu import COPY_FAILED_TEXT, has_window_keyboard_focus, place_menu, place_secondary_bar

SAVED_PREFIX = "✓ 已保存 "
SAVE_FAILED_PREFIX = "保存失败："


class SaveResultLike(Protocol):
    """业务层保存回调返回协议（对齐 `_desktop.capture_flow.SaveResult`）。"""

    @property
    def path(self) -> str | None: ...

    @property
    def error(self) -> str | None: ...


class AnnotateOverlay(QWidget):
    """全屏透明截屏标注浮窗：冻结图 + 标注画布 + 模板 B 双条零抖动工具条。"""

    def __init__(
        self,
        cropped_png: bytes,
        screen_rect: tuple[int, int, int, int],
        *,
        virtual_geometry: tuple[int, int, int, int] | None = None,
        on_copy_image: Callable[[bytes], bool] | None = None,
        on_save_image: Callable[[bytes], SaveResultLike] | None = None,
        on_closed: Callable[[], None] | None = None,
        initial_style: tuple[str, str] | None = None,
        on_style_changed: Callable[[tuple[str, str]], None] | None = None,
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

        if initial_style is None:
            color_hex, size_spec = theme.resolve_annotate_style()
        else:
            if not isinstance(initial_style, tuple) or len(initial_style) != 2:
                raise ValueError(f"initial_style 必须是二元组 (color_hex, size_level)，收到 {initial_style!r}")
            color_hex, size_spec = theme.resolve_annotate_style(initial_style[0], initial_style[1])

        self._base = base.convertToFormat(QImage.Format.Format_ARGB32)
        self._screen_rect = QRect(sx, sy, sw, sh)
        self._origin = QPoint(vx, vy)
        self._canvas = QRect(sx - vx, sy - vy, sw, sh)
        self._avail = theme.resolve_available_screen_rect(available_screen_rect, QPoint(sx, sy))

        self._on_copy_image = on_copy_image
        self._on_save_image = on_save_image
        self._on_closed = on_closed
        self._on_style_changed = on_style_changed
        self._copy_feedback_ms = max(1, int(copy_feedback_ms))
        self._saved_feedback_ms = max(1, int(saved_feedback_ms))
        self._closed_notified = False

        self._color: str = color_hex
        self._size_spec: theme.AnnotateSizeSpec = size_spec
        self._tool: ToolName | None = None
        self._ops: list[AnnotationOp] = []
        self._composite: QImage = self._base.copy()
        self._draft: AnnotationOp | None = None
        self._text_edit: InlineTextEdit | None = None
        self._text_anchor: QPointF | None = None
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
            on_pick_tool=lambda key: self.pick_tool(key, toggle=True),
            on_undo=self.undo,
            on_copy=self._copy_image,
            on_save=self._save_image,
            on_cancel=self._cancel_and_close,
            parent=self,
        )
        self.style_bar = AnnotateStyleBar(
            on_pick_color=self.pick_color,
            on_pick_size=self.pick_size,
            parent=self,
        )
        self._place_toolbar()

    # ------------------------------------------------------------------
    # 公共接口（capture_flow.AnnotatorHandle 与状态查询）
    # ------------------------------------------------------------------
    @property
    def menu_placement(self) -> str:
        """主工具条当前位置：below / above / inside。"""
        return self._menu_placement

    @property
    def current_tool(self) -> ToolName | None:
        return self._tool

    @property
    def current_color(self) -> str:
        return self._color

    @property
    def current_size_level(self) -> str:
        return self._size_spec.key

    @property
    def current_style(self) -> tuple[str, str]:
        return (self._color, self._size_spec.key)

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

    def pick_tool(self, tool: str, *, toggle: bool = False) -> None:
        if tool not in VALID_TOOLS:
            raise ValueError(f"不支持的标注工具：{tool!r}，允许 {sorted(VALID_TOOLS)}")
        self._draft = None
        if self._text_edit is not None:
            self._commit_text()
        typed_tool = cast(ToolName, tool)
        self._tool = None if (toggle and self._tool == typed_tool) else typed_tool
        self.toolbar.set_active_tool(self._tool)
        self._sync_style_bar()
        self.update()

    def pick_color(self, color: str) -> None:
        validated = theme.validate_annotate_color(color)
        changed = validated != self._color
        self._color = validated
        if self._text_edit is not None:
            self._text_edit.apply_style(color=self._color, text_px=self._size_spec.text_px)
        self._sync_style_bar()
        if changed and self._on_style_changed is not None:
            self._on_style_changed(self.current_style)

    def pick_size(self, size_level: str) -> None:
        spec = theme.validate_annotate_size(size_level)
        changed = spec.key != self._size_spec.key
        self._size_spec = spec
        if self._text_edit is not None and self._text_anchor is not None:
            self._text_edit.apply_style(color=self._color, text_px=self._size_spec.text_px)
            self._text_anchor = self._text_edit.fit_in_canvas(self._canvas, self._text_anchor)
        self._sync_style_bar()
        if changed and self._on_style_changed is not None:
            self._on_style_changed(self.current_style)

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
        self._draft = None
        self._cancel_text()
        self.close()

    # ------------------------------------------------------------------
    # 工具条动作与零抖动定位：复制 / 保存 / 取消 / Esc / Enter
    # ------------------------------------------------------------------
    def _handle_escape_action(self) -> None:
        if self._text_edit is not None:
            self._cancel_text()
            return
        self.close()

    def _cancel_and_close(self) -> None:
        self._draft = None
        self._cancel_text()
        self.close()

    def _reset_feedback(self) -> None:
        """原子重置既有复制/保存反馈状态与双计时器（保证四态互斥，且回调异常前已同步重排）。"""
        self._stop_timers()
        self.toolbar.set_copy_failed(False)
        if self.toolbar.status_text():
            self.toolbar.set_status("", ok=True)
            self._place_toolbar()

    def _copy_image(self) -> None:
        self._reset_feedback()
        if self._text_edit is not None:
            self._commit_text()
        self._finalize_draft()
        ok = bool(self._on_copy_image(self.export_png())) if self._on_copy_image is not None else False
        if ok:
            self.close()
            return
        self.toolbar.set_copy_failed(True)
        self.toolbar.set_status(COPY_FAILED_TEXT, ok=False)
        self._place_toolbar()
        self._copy_reset_timer.start(self._copy_feedback_ms)

    def _clear_copy_failed(self) -> None:
        self.toolbar.set_copy_failed(False)
        if self.toolbar.status_text() == COPY_FAILED_TEXT:
            self.toolbar.set_status("", ok=True)
            self._place_toolbar()

    def _save_image(self) -> None:
        self._reset_feedback()
        if self._text_edit is not None:
            self._commit_text()
        self._finalize_draft()
        if self._on_save_image is None:
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
        self.toolbar.set_status(f"{SAVE_FAILED_PREFIX}{result.error}", ok=False)
        self._place_toolbar()

    def _place_toolbar(self) -> None:
        self.toolbar.adjustSize()
        reserve_h = self.style_bar.reserved_height() + theme.ANNOTATE_SUB_BAR_GAP_PX
        top_left, placement = place_menu(self._screen_rect, self.toolbar.size(), self._avail, reserve_extra_h=reserve_h)
        self.toolbar.move(top_left - self._origin)
        self._menu_placement = placement
        if self.style_bar.isVisible():
            self._place_style_bar()

    def _place_style_bar(self) -> None:
        self.style_bar.adjustSize()
        main_global = QRect(self.toolbar.pos() + self._origin, self.toolbar.size())
        sub_top_left = place_secondary_bar(main_global, self.style_bar.size(), self._menu_placement, self._avail)
        self.style_bar.move(sub_top_left - self._origin)

    def _sync_style_bar(self) -> None:
        self.style_bar.set_state(tool=self._tool, color=self._color, size_level=self._size_spec.key)
        if self.style_bar.isVisible():
            self._place_style_bar()

    def _stop_timers(self) -> None:
        self._copy_reset_timer.stop()
        self._save_close_timer.stop()

    # ------------------------------------------------------------------
    # 文字内联编辑与标注提交
    # ------------------------------------------------------------------
    def _open_text(self, canvas_pt: QPointF) -> None:
        self._cancel_text()
        edit = InlineTextEdit(
            color=self._color,
            text_px=self._size_spec.text_px,
            on_commit=self._commit_text,
            on_cancel=self._cancel_text,
            parent=self,
        )
        self._text_edit = edit
        self._text_anchor = edit.fit_in_canvas(self._canvas, canvas_pt)
        edit.textChanged.connect(lambda _text: self._fit_text_edit())
        edit.show()
        edit.setFocus(Qt.FocusReason.MouseFocusReason)

    def _fit_text_edit(self) -> None:
        edit = self._text_edit
        anchor = self._text_anchor
        if edit is None or anchor is None:
            return
        self._text_anchor = edit.fit_in_canvas(self._canvas, anchor)

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
            self._commit_op(
                make_op(TOOL_TEXT, anchor, anchor, color=self._color, size_level=self._size_spec.key, text=text)
            )

    def _cancel_text(self) -> None:
        self._detach_text_edit()

    def _finalize_draft(self) -> None:
        draft = self._draft
        if draft is None:
            return
        self._draft = None
        if is_valid_op(draft):
            self._commit_op(draft)
        else:
            self.update()

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
    # 鼠标与键盘事件（点框外不关闭；inside 模式下主条/可见二级条防穿透）
    # ------------------------------------------------------------------
    def _point_on_bars(self, pos: QPoint) -> bool:
        if self.toolbar.geometry().contains(pos):
            return True
        return self.style_bar.isVisible() and self.style_bar.geometry().contains(pos)

    def _to_canvas(self, pos: QPointF) -> QPointF:
        return clamp_point(
            QPointF(pos.x() - self._canvas.x(), pos.y() - self._canvas.y()),
            self._canvas.width(),
            self._canvas.height(),
        )

    def _update_cursor(self, pos: QPoint) -> None:
        if self._point_on_bars(pos) or not self._canvas.contains(pos) or self._tool is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._tool == TOOL_TEXT:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        pos = event.position().toPoint()
        if self._point_on_bars(pos):
            return
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
        self._draft = make_op(self._tool, pt, pt, color=self._color, size_level=self._size_spec.key)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._update_cursor(event.position().toPoint())
        if self._draft is not None:
            pt = self._to_canvas(event.position())
            if self._draft.tool == TOOL_BRUSH:
                updated = append_brush_point(self._draft, pt)
                if updated is not self._draft:
                    self._draft = updated
                    self.update()
            else:
                self._draft = replace(self._draft, end=pt)
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._draft is None:
            event.accept()
            return
        pt = self._to_canvas(event.position())
        if self._draft.tool == TOOL_BRUSH:
            self._draft = append_brush_point(self._draft, pt, force=True)
        else:
            self._draft = replace(self._draft, end=pt)
        self._finalize_draft()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._handle_escape_action()
            return
        if is_plain_enter_event(event):
            event.accept()
            if self._text_edit is not None:
                self._commit_text()
            else:
                self._copy_image()
            return
        event.ignore()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_timers()
        self._draft = None
        self._cancel_text()
        if not self._closed_notified:
            self._closed_notified = True
            if self._on_closed is not None:
                self._on_closed()
        super().closeEvent(event)
