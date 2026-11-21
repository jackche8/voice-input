"""原位识别 / 翻译浮窗的框下迷你菜单（UI 验收标准 docs/design/v3.3/overlay-a.html）。

单一职责：按模式与状态出按钮（OCR「复制」/ 翻译「复制译文 · 复制原文」/ 失败「重试」/
加载中转圈提示），按复制回调的返回值如实显示「✓ 已复制」或「复制失败」，
并给出「框下 → 框上 → 框内底部」的避让位置。纯 UI：QFrame + QPushButton，
样式来自 `theme.float_bar_stylesheet()`，通过回调派发意图。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QHideEvent, QPainter, QPaintEvent, QPen, QShowEvent
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from _ui_desktop import theme

COPY_TRANSLATED = "translated"
COPY_OCR = "ocr"
COPY_LABELS: dict[str, dict[str, str]] = {
    "ocr": {COPY_OCR: "复制"},
    "translate": {COPY_TRANSLATED: "复制译文", COPY_OCR: "复制原文"},
}
LOADING_TEXT = {"ocr": "识别中…", "translate": "识别并翻译中…"}
COPIED_TEXT = "✓ 已复制"
COPY_FAILED_TEXT = "复制失败"
RETRY_TEXT = "重试"
ESC_HINT = "Esc"
UNDO_TEXT = "撤销"
COPY_TEXT = "复制"
SAVE_TEXT = "保存"
CANCEL_TEXT = "取消"


class _Spinner(QWidget):
    """加载中的旋转弧，只在可见时转动。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(theme.OVERLAY_SPINNER_PX, theme.OVERLAY_SPINNER_PX)
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(theme.OVERLAY_FRAME_MS)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._angle = (self._angle + 360.0 * theme.OVERLAY_FRAME_MS / theme.OVERLAY_SPIN_PERIOD_MS) % 360.0
        self.update()

    def showEvent(self, event: QShowEvent) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(theme.qcolor(theme.ACCENT_HEX, opaque=True), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(self.rect()).adjusted(1, 1, -1, -1), int(-self._angle * 16), -270 * 16)
        painter.end()


class MiniMenu(QFrame):
    """框下迷你菜单：加载 / 结果 / 失败三种内容，复制按钮按回调结果反馈 1.5s。"""

    def __init__(
        self,
        mode: str,
        *,
        on_copy: Callable[[str], bool],
        on_retry: Callable[[], None],
        feedback_ms: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._on_copy = on_copy
        self._feedback_ms = max(100, int(feedback_ms))
        self.setObjectName("oviFloatBar")
        self.setStyleSheet(theme.float_bar_stylesheet())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(4)

        self._status_box = QWidget(self)
        status_layout = QHBoxLayout(self._status_box)
        status_layout.setContentsMargins(8, 0, 6, 0)
        status_layout.setSpacing(6)
        self._spinner = _Spinner(self._status_box)
        self._status = QLabel(self._status_box)
        status_layout.addWidget(self._spinner)
        status_layout.addWidget(self._status)
        layout.addWidget(self._status_box)

        self._copy_buttons: dict[str, QPushButton] = {}
        self._restore_timers: dict[str, QTimer] = {}
        for key, label in COPY_LABELS[mode].items():
            button = self._make_button(label)
            button.clicked.connect(lambda _checked=False, k=key: self._copy(k))
            layout.addWidget(button)
            self._copy_buttons[key] = button
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda k=key: self._clear_feedback(k))
            self._restore_timers[key] = timer

        self._retry = make_bar_button(self, RETRY_TEXT)
        self._retry.clicked.connect(on_retry)
        layout.addWidget(self._retry)

        layout.addWidget(make_bar_divider(self))
        hint = QLabel(ESC_HINT, self)
        hint.setObjectName("oviFloatHint")
        hint.setContentsMargins(4, 0, 8, 0)
        layout.addWidget(hint)

    def _make_button(self, label: str) -> QPushButton:
        return make_bar_button(self, label)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def button(self, key: str) -> QPushButton:
        """复制按钮（`COPY_OCR` / `COPY_TRANSLATED`）或重试按钮（`"retry"`）。"""
        return self._retry if key == "retry" else self._copy_buttons[key]

    def visible_labels(self) -> list[str]:
        """当前可见的按钮文字（按从左到右顺序）。"""
        buttons = [*self._copy_buttons.values(), self._retry]
        return [b.text() for b in buttons if not b.isHidden()]

    def status_text(self) -> str:
        return "" if self._status_box.isHidden() else self._status.text()

    def show_loading(self) -> None:
        self._set_status(LOADING_TEXT[self._mode], error=False)
        self._set_buttons(copy_visible=False, retry_visible=False)

    def show_error(self, reason: str) -> None:
        self._set_status(f"✕ {reason}", error=True)
        self._set_buttons(copy_visible=False, retry_visible=True)

    def show_result(self, copyable: dict[str, bool]) -> None:
        """结果态：显示复制按钮；没有可复制文本的按钮置灰。"""
        self._status_box.hide()
        self._set_buttons(copy_visible=True, retry_visible=False)
        for key, button in self._copy_buttons.items():
            button.setEnabled(copyable[key])
        self.adjustSize()

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status.setObjectName("oviFloatError" if error else "oviFloatStatus")
        self._status.setText(text)
        self._spinner.setVisible(not error)
        self._status_box.show()
        self.setStyleSheet(theme.float_bar_stylesheet())
        self.adjustSize()

    def _set_buttons(self, *, copy_visible: bool, retry_visible: bool) -> None:
        for key, button in self._copy_buttons.items():
            self._clear_feedback(key)
            button.setVisible(copy_visible)
        self._retry.setVisible(retry_visible)

    # ------------------------------------------------------------------
    # 复制反馈
    # ------------------------------------------------------------------
    def _copy(self, key: str) -> None:
        ok = bool(self._on_copy(key))
        button = self._copy_buttons[key]
        button.setText(COPIED_TEXT if ok else COPY_FAILED_TEXT)
        self._set_feedback_style(button, "ok" if ok else "fail")
        self._restore_timers[key].start(self._feedback_ms)
        self.adjustSize()

    def _clear_feedback(self, key: str) -> None:
        self._restore_timers[key].stop()
        button = self._copy_buttons[key]
        button.setText(COPY_LABELS[self._mode][key])
        self._set_feedback_style(button, "")
        self.adjustSize()

    @staticmethod
    def _set_feedback_style(button: QPushButton, feedback: str) -> None:
        set_button_property(button, "feedback", feedback)


def make_bar_button(parent: QWidget, label: str) -> QPushButton:
    """构建悬浮工具条无焦点按钮（原位迷你菜单与标注工具条共用）。"""
    button = QPushButton(label, parent)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def make_bar_divider(parent: QWidget) -> QFrame:
    """构建悬浮工具条 1×16px 纵向分隔线。"""
    divider = QFrame(parent)
    divider.setObjectName("oviFloatDivider")
    divider.setFixedSize(1, 16)
    return divider


def set_button_property(button: QPushButton, name: str, value: str) -> None:
    """更新按钮动态 QSS 属性（如 active / feedback）并刷新样式。"""
    button.setProperty(name, value)
    button.style().unpolish(button)
    button.style().polish(button)


def has_window_keyboard_focus(window: QWidget) -> bool:
    """窗口是否持有键盘焦点：自身为活动窗且当前焦点控件是本窗或其子控件。"""
    if not window.isActiveWindow():
        return False
    focus = QApplication.focusWidget()
    return focus is not None and (focus is window or window.isAncestorOf(focus))


def place_menu(canvas: QRect, size: QSize, avail: QRect) -> tuple[QPoint, str]:
    """菜单左上角（全局坐标）与位置名：框下 → 放不下翻到框上 → 再放不下放框内底部；水平不出屏。"""
    right_limit = avail.x() + avail.width() - size.width()
    x = max(avail.x(), min(canvas.x(), right_limit))
    below = canvas.y() + canvas.height() + theme.OVERLAY_MENU_GAP_PX
    if below + size.height() <= avail.y() + avail.height():
        return QPoint(x, below), "below"
    above = canvas.y() - theme.OVERLAY_MENU_GAP_PX - size.height()
    if above >= avail.y():
        return QPoint(x, above), "above"
    inside = canvas.y() + canvas.height() - size.height() - theme.OVERLAY_MENU_INSIDE_PAD_PX
    return QPoint(x, max(canvas.y(), inside)), "inside"


class AnnotateToolbar(QFrame):
    """截屏标注迷你工具条（annotate-a.html）：矩形·箭头·文字·马赛克 │ 撤销 │ 复制·保存 │ 取消 Esc [状态]。"""

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
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(4)

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

        hint = QLabel(ESC_HINT, self)
        hint.setObjectName("oviFloatHint")
        hint.setContentsMargins(2, 0, 8, 0)
        layout.addWidget(hint)

        self._status = QLabel("", self)
        self._status.setObjectName("oviFloatOk")
        self._status.setContentsMargins(8, 0, 8, 0)
        self._status.hide()
        layout.addWidget(self._status)

    def button(self, key: str) -> QPushButton:
        """按工具名或动作名（rect/arrow/text/mosaic/undo/copy/save/cancel）取按钮。"""
        return self._all_buttons[key]

    def visible_labels(self) -> list[str]:
        """当前可见按钮的从左到右文案列表。"""
        return [btn.text() for btn in self._all_buttons.values() if not btn.isHidden()]

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
