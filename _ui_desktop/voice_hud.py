"""语音输入状态胶囊与未确认投递决策卡片（方案 C）。

单一职责：
1. 管理屏幕左下角真透明状态胶囊（`idle` 默认隐藏，支持录音秒表、润色、忙碌、错误、Toast）；
2. 管理 `delivery_unknown` 时的可点击重发确认卡片（唯一开启鼠标点击态，仍保持不抢夺键盘焦点）；
3. 组合并协同底部居中双行大字幕窗口 `StreamingCaptionBar`（独立定义于 `caption_bar.py`）。
"""
from __future__ import annotations

import inspect
import time
from collections.abc import Callable

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from _ui_desktop import theme
from _ui_desktop.caption_bar import (
    CAPTION_AUTO_HIDE_MS,
    HUD_BOTTOM_MARGIN_PX,
    PASSIVE_WINDOW_FLAGS,
    StreamingCaptionBar,
    compute_trailing_two_lines,
    normalize_single_line,
    passive_overlay_flags,
)

__all__ = [
    "CAPTION_AUTO_HIDE_MS",
    "StreamingCaptionBar",
    "VoiceHudOverlay",
    "compute_trailing_two_lines",
]

CAPSULE_LEFT_MARGIN_PX = 20

INTERACTIVE_NO_FOCUS_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
)


class VoiceHudOverlay(QWidget):
    """左下角轻量状态胶囊 + 底部居中双行大字幕条总控视图。"""

    def __init__(
        self,
        *,
        available_screen_rect: tuple[int, int, int, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._available_screen_override = available_screen_rect
        self._clock = clock
        self._enabled: bool = True

        self._mode: str = "idle"
        self._capsule_text: str = ""
        self._dot_hex: str = theme.STATE_IDLE_HEX
        self._interactive: bool = False
        self._record_started_at: float = 0.0

        self._confirm_job_id: str | None = None
        self._confirm_text: str = ""
        self._confirm_callback: Callable[..., None] | None = None

        self._capsule_font: QFont = theme.make_font(
            pixel_size=theme.FS_XS,
            weight=QFont.Weight.Medium,
        )

        self.caption_bar = StreamingCaptionBar(
            available_screen_rect=available_screen_rect,
        )

        self._build_confirm_card()
        self._apply_interactivity(False)

        self._record_tick_timer = QTimer(self)
        self._record_tick_timer.setInterval(500)
        self._record_tick_timer.timeout.connect(self._on_record_tick)

        self._capsule_hide_timer = QTimer(self)
        self._capsule_hide_timer.setSingleShot(True)
        self._capsule_hide_timer.timeout.connect(self._hide_capsule_only)

        self.hide()

    def _build_confirm_card(self) -> None:
        self.confirm_frame = QFrame(self)
        self.confirm_frame.setObjectName("oviFloatBar")
        self.confirm_frame.setStyleSheet(theme.float_bar_stylesheet())

        layout = QHBoxLayout(self.confirm_frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.confirm_label = QLabel(self.confirm_frame)
        self.confirm_label.setFont(self._capsule_font)
        self.confirm_label.setStyleSheet(
            f"color: {theme.CAPSULE_TEXT_HEX}; background: transparent; border: none;"
        )
        layout.addWidget(self.confirm_label)

        self.confirm_button = QPushButton("重新输入", self.confirm_frame)
        self.confirm_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.confirm_button.clicked.connect(lambda: self._resolve_confirmation(True))
        layout.addWidget(self.confirm_button)

        self.cancel_button = QPushButton("忽略", self.confirm_frame)
        self.cancel_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cancel_button.clicked.connect(lambda: self._resolve_confirmation(False))
        layout.addWidget(self.cancel_button)

        self.confirm_frame.setVisible(False)

    def _apply_interactivity(self, interactive: bool) -> None:
        self._interactive = interactive
        flags = INTERACTIVE_NO_FOCUS_FLAGS if interactive else passive_overlay_flags()
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not interactive)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def capsule_text(self) -> str:
        return self._capsule_text

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_mouse_passthrough(self) -> bool:
        """当前状态胶囊是否处于鼠标穿透且不抢焦点的被动悬浮模式。"""
        return (
            not self._interactive
            and bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            and bool(self.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        )

    def set_enabled(self, enabled: bool) -> None:
        """启用/关闭桌面状态提示（供托盘勾选菜单切换）。"""
        self._enabled = bool(enabled)
        self.caption_bar.set_enabled(self._enabled)
        if not self._enabled:
            self.hide()
        elif self._mode != "idle":
            self.show()

    def set_idle(self) -> None:
        """恢复空闲：胶囊立即隐藏；若大字幕仍在展示，则进入 1.5s 淡出倒计时。"""
        self._stop_capsule_timers()
        self._clear_confirm_state()
        self._mode = "idle"
        self._capsule_text = ""
        self._dot_hex = theme.STATE_IDLE_HEX
        if self._interactive:
            self._apply_interactivity(False)
        self.hide()
        if self.caption_bar.isVisible() and not self.caption_bar.is_auto_hide_active():
            self.caption_bar.finish(hold_ms=CAPTION_AUTO_HIDE_MS)

    def set_recording(self, elapsed_s: float | None = None) -> None:
        """进入录音态：左下角展示红色圆点 + 实时秒表；若上一句字幕正处收尾倒计时则先清空。"""
        self._stop_capsule_timers()
        self._clear_confirm_state()
        if self.caption_bar.is_auto_hide_active():
            self.caption_bar.clear()
        now = self._clock()
        self._record_started_at = now - max(0.0, float(elapsed_s or 0.0))
        self._mode = "recording"
        self._dot_hex = theme.STATE_RECORDING_HEX
        self._capsule_text = f"录音中 {self._format_duration(now - self._record_started_at)}"
        self._show_passive_capsule()
        self._record_tick_timer.start()

    def set_polishing(self) -> None:
        """进入本地 LLM 纠错润色态。"""
        self._enter_passive_state("polishing", theme.STATE_POLISHING_HEX, "纠错润色中…")

    def set_busy(self, message: str = "处理中…") -> None:
        """进入通用忙碌态（如截图准备或队列处理中）。"""
        self._enter_passive_state("busy", theme.STATE_BUSY_HEX, message or "处理中…")

    def set_error(self, msg: str, duration_ms: int = 3500) -> None:
        """展示错误胶囊提示；若正在显示流式字幕则触发 1.5s 收起。"""
        self._enter_passive_state(
            "error",
            theme.STATE_ERROR_HEX,
            msg or "操作失败",
            duration_ms=duration_ms,
        )
        if self.caption_bar.isVisible() and not self.caption_bar.is_auto_hide_active():
            self.caption_bar.finish(hold_ms=CAPTION_AUTO_HIDE_MS)

    def show_toast(
        self,
        msg: str,
        kind: str = "ok",
        duration_ms: int = 1800,
    ) -> None:
        """展示轻量结果胶囊（如「已复制 OCR 文本（42 字）」），到期自动隐藏。"""
        kind_colors = {
            "ok": theme.STATE_OK_HEX,
            "warn": theme.STATE_WARN_HEX,
            "error": theme.STATE_ERROR_HEX,
            "info": theme.ACCENT_HEX,
        }
        self._enter_passive_state(
            "toast",
            kind_colors.get(kind, theme.STATE_OK_HEX),
            msg,
            duration_ms=duration_ms,
        )

    def show_delivery_confirmation(
        self,
        job_id: str,
        text: str,
        on_decision: Callable[..., None],
    ) -> None:
        """展示可点击的 `delivery_unknown` 重发确认卡片（唯一开启鼠标点击态，仍不抢键盘焦点）。"""
        self._stop_capsule_timers()
        self._mode = "confirm"
        self._dot_hex = theme.STATE_WARN_HEX
        self._confirm_job_id = job_id
        self._confirm_text = text
        self._confirm_callback = on_decision

        preview = normalize_single_line(text).strip()
        if len(preview) > 24:
            preview = f"{preview[:24]}…"
        prompt = (
            f"未确认是否已输入「{preview}」，是否重发？"
            if preview
            else "未确认文字是否已输入，是否重发？"
        )
        self._capsule_text = prompt
        self.confirm_label.setText(prompt)
        self.confirm_frame.setVisible(True)
        self.confirm_frame.adjustSize()

        if not self._interactive:
            self._apply_interactivity(True)

        hint = self.confirm_frame.sizeHint()
        card_w = max(280, hint.width())
        card_h = max(40, hint.height())
        self._position_bottom_left(card_w, card_h)
        self.confirm_frame.setGeometry(0, 0, card_w, card_h)

        if self._enabled:
            self.show()
            self.update()

    # ------------------------------------------------------------------
    # 流式双行大字幕编排委托
    # ------------------------------------------------------------------
    def update_caption(self, stable_text: str = "", tail_text: str = "") -> None:
        self.caption_bar.set_streaming_text(stable_text=stable_text, tail_text=tail_text)

    def set_partial(self, stable_text: str = "", tail_text: str = "") -> None:
        self.update_caption(stable_text=stable_text, tail_text=tail_text)

    def finish_caption(
        self,
        final_text: str | None = None,
        hold_ms: int = CAPTION_AUTO_HIDE_MS,
    ) -> None:
        self.caption_bar.finish(final_text=final_text, hold_ms=hold_ms)

    def set_typed(
        self,
        text: str = "",
        hold_ms: int = CAPTION_AUTO_HIDE_MS,
    ) -> None:
        self.caption_bar.finish(final_text=text if text else None, hold_ms=hold_ms)
        self.show_toast("✓ 已输入", kind="ok", duration_ms=1500)

    def clear_caption(self) -> None:
        self.caption_bar.clear()

    def visible_caption_lines(self) -> list[tuple[str, str]]:
        return self.caption_bar.visible_lines()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _enter_passive_state(
        self,
        mode: str,
        dot_hex: str,
        text: str,
        *,
        duration_ms: int = 0,
    ) -> None:
        self._stop_capsule_timers()
        self._clear_confirm_state()
        self._mode = mode
        self._dot_hex = dot_hex
        self._capsule_text = text
        self._show_passive_capsule()
        if duration_ms > 0:
            self._capsule_hide_timer.start(int(duration_ms))

    def _show_passive_capsule(self) -> None:
        self.confirm_frame.setVisible(False)
        if self._interactive:
            self._apply_interactivity(False)

        fm = QFontMetrics(self._capsule_font)
        avail = theme.resolve_available_screen_rect(self._available_screen_override)
        max_w = max(180, min(460, avail.width() - 2 * CAPSULE_LEFT_MARGIN_PX))
        text_w = fm.horizontalAdvance(self._capsule_text)
        pill_w = min(max_w, max(96, text_w + 38))
        pill_h = max(28, fm.height() + 12)

        self._position_bottom_left(pill_w, pill_h)
        if self._enabled:
            if not self.isVisible():
                self.show()
            self.update()

    def _position_bottom_left(self, width: int, height: int) -> None:
        avail = theme.resolve_available_screen_rect(self._available_screen_override)
        x = avail.x() + CAPSULE_LEFT_MARGIN_PX
        y = avail.y() + avail.height() - height - HUD_BOTTOM_MARGIN_PX
        target = QRect(x, y, width, height)
        if self.geometry() != target:
            self.setGeometry(target)

    def _on_record_tick(self) -> None:
        if self._mode != "recording":
            self._record_tick_timer.stop()
            return
        elapsed = max(0.0, self._clock() - self._record_started_at)
        new_text = f"录音中 {self._format_duration(elapsed)}"
        if new_text == self._capsule_text:
            return
        self._capsule_text = new_text
        self._show_passive_capsule()

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        mins, secs = divmod(total, 60)
        return f"{mins}:{secs:02d}"

    def _hide_capsule_only(self) -> None:
        self._stop_capsule_timers()
        if self._mode != "confirm":
            self._mode = "idle"
            self._capsule_text = ""
            self.hide()

    def _stop_capsule_timers(self) -> None:
        self._record_tick_timer.stop()
        self._capsule_hide_timer.stop()

    def _clear_confirm_state(self) -> None:
        self._confirm_job_id = None
        self._confirm_text = ""
        self._confirm_callback = None
        self.confirm_frame.setVisible(False)

    def _resolve_confirmation(self, confirmed: bool) -> None:
        job_id = self._confirm_job_id or ""
        callback = self._confirm_callback
        self._clear_confirm_state()
        self._mode = "idle"
        self._capsule_text = ""
        self._apply_interactivity(False)
        self.hide()
        if callback is not None:
            self._invoke_decision_callback(callback, job_id, confirmed)

    @staticmethod
    def _invoke_decision_callback(
        callback: Callable[..., None],
        job_id: str,
        confirmed: bool,
    ) -> None:
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError):
            callback(confirmed)
            return

        positional = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in sig.parameters.values()
        )
        if len(positional) >= 2 and not has_varargs:
            callback(job_id, confirmed)
        else:
            callback(confirmed)

    def paintEvent(self, _event: QPaintEvent) -> None:
        if self._mode in {"idle", "confirm"} or not self._capsule_text:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect_f = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        radius = (h - 1.0) / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect_f, radius, radius)
        painter.fillPath(path, theme.qcolor(theme.CAPSULE_BG_RGBA))
        painter.setPen(QPen(theme.qcolor(theme.CAPSULE_BORDER_HEX, opaque=True), 1.0))
        painter.drawPath(path)

        dot_size = 8.0
        dot_x = 12.0
        dot_y = (h - dot_size) / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(self._dot_hex, opaque=True))
        painter.drawEllipse(QRectF(dot_x, dot_y, dot_size, dot_size))

        text_x = int(dot_x + dot_size + 7)
        text_rect = QRect(text_x, 0, max(10, w - text_x - 12), h)
        painter.setFont(self._capsule_font)
        painter.setPen(theme.qcolor(theme.CAPSULE_TEXT_HEX, opaque=True))
        fm = QFontMetrics(self._capsule_font)
        elided = fm.elidedText(
            self._capsule_text,
            Qt.TextElideMode.ElideRight,
            text_rect.width(),
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            elided,
        )
        painter.end()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_capsule_timers()
        self.caption_bar.clear()
        self.caption_bar.close()
        super().closeEvent(event)
