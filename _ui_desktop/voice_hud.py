"""语音输入一体底栏公共外观（UI 验收标准 docs/design/v3.3/voice-a.html）。

单一职责：对外保持 `VoiceHudOverlay` 公共方法，把语音事件转成一体底栏的状态
（胶囊 / 字幕条 / 确认卡片三种形态，录音中的警告走附加提示行），驱动动效与重绘，
接线确认卡片点击。绘制见 `voice_bar_view.py`，动效时刻见 `voice_bar_motion.py`。
窗口按最大尺寸固定在屏幕底部居中，动画只改绘制区域；默认鼠标穿透且不抢焦点，
只有确认卡片区域接收点击。
"""
from __future__ import annotations

import inspect
import time
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QCloseEvent, QMouseEvent, QPainter, QPaintEvent, QRegion
from PySide6.QtWidgets import QWidget

from _ui_desktop import theme
from _ui_desktop import voice_bar_view as view
from _ui_desktop.caption_bar import (
    CAPTION_AUTO_HIDE_MS,
    HUD_BOTTOM_MARGIN_PX,
    compute_trailing_two_lines,
    normalize_single_line,
    passive_overlay_flags,
)
from _ui_desktop.voice_bar_motion import BarMotion, FrameTicker, breathe, spin_degrees

__all__ = ["VoiceHudOverlay"]

INTERACTIVE_NO_FOCUS_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
)
TOAST_HEX = {
    "ok": theme.STATE_OK_HEX,
    "warn": theme.STATE_WARN_HEX,
    "error": theme.STATE_ERROR_HEX,
    "info": theme.ACCENT_HEX,
}
CONFIRM_PREVIEW_CHARS = 24
TYPED_TEXT = "✓ 已输入"
TYPED_LABEL = "已输入"
RECORDING_SIZING_LABEL = "录音中 0:00"


class VoiceHudOverlay(QWidget):
    """底部居中一体底栏：录音秒表、流式两行字幕、润色、已输入、错误、提示与确认卡片。"""

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
        self._enabled = True
        self._interactive = False
        self._fonts = view.BarFonts()
        self._motion = BarMotion()
        self._ticker = FrameTicker(self._on_frame, self)
        self._reset_state()
        self._apply_interactivity(False)
        self.hide()

    def _reset_state(self) -> None:
        self._mode = "idle"
        self._typed = False
        self._form: str | None = None
        self._capsule_text = ""
        self._msg = ""
        self._record_started_at = 0.0
        self._stable = ""
        self._tail = ""
        self._spans: list[tuple[int, int]] = []
        self._swap: tuple[str, tuple[tuple[int, int], ...]] | None = None
        self._notice_text = ""
        self._hold_until: float | None = None
        self._confirm: tuple[str, Callable[..., None]] | None = None
        self._hover: str | None = None

    # ------------------------------------------------------------------
    # 公共属性
    # ------------------------------------------------------------------
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
        """当前是否处于鼠标穿透且不抢焦点的被动悬浮模式。"""
        return (
            not self._interactive
            and bool(self.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            and bool(self.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        )

    # ------------------------------------------------------------------
    # 公共方法（签名与 v3.2 一致）
    # ------------------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        """启用/关闭桌面状态提示（供托盘勾选菜单切换）。"""
        self._enabled = bool(enabled)
        if not self._enabled:
            self.hide()
        elif self._mode != "idle":
            self._present()

    def set_idle(self) -> None:
        """恢复空闲：录音/润色/确认等进行中的状态淡出；已输入、提示、错误按各自保持时间结束。"""
        if self._mode in ("idle", "toast", "error"):
            return
        self._begin_exit(self._now())

    def set_recording(self, elapsed_s: float | None = None) -> None:
        """进入录音：红点呼吸 + 秒表；清空上一句字幕与附加提示行。"""
        t = self._now()
        self._enter("recording", t, theme.STATE_RECORDING_HEX)
        self._record_started_at = t - max(0.0, float(elapsed_s or 0.0)) * 1000.0
        self._stable = self._tail = ""
        self._spans = []
        self._swap = None
        self._notice_text = ""
        self._motion.clear_notice()
        self._motion.border.set(theme.qcolor(theme.CAPTION_BORDER_RGBA), t)
        self._capsule_text = ""
        self._on_record_tick()
        self._relayout(t)
        self._present()

    def set_polishing(self) -> None:
        """进入本地 LLM 纠错润色：尾巴转为已确认，状态列转为旋转弧。"""
        t = self._now()
        self._enter("polishing", t, theme.STATE_POLISHING_HEX)
        merged = self._stable + self._tail
        if len(merged) > len(self._stable):
            self._motion.mark_fresh(len(self._stable), len(merged), t)
        self._stable, self._tail = merged, ""
        self._motion.border.set(view.with_alpha(theme.STATE_POLISHING_HEX, theme.VOICE_SOFT_BORDER_ALPHA), t)
        self._capsule_text = "纠错润色中…"
        self._relayout(t)
        self._present()

    def set_error(self, msg: str, duration_ms: int = 3500) -> None:
        """错误胶囊（录音中也一律切到错误态）；duration_ms<=0 时不自动结束。"""
        t = self._now()
        self._enter("error", t, theme.STATE_ERROR_HEX)
        self._msg = self._capsule_text = msg or "操作失败"
        self._motion.border.set(view.with_alpha(theme.STATE_ERROR_HEX, theme.VOICE_FIRM_BORDER_ALPHA), t)
        self._relayout(t)
        self._hold(t, duration_ms)
        self._present()

    def show_toast(self, msg: str, kind: str = "ok", duration_ms: int = 1800) -> None:
        """单行提示胶囊；录音中的警告改为附加提示行，不改变状态、秒表与字幕。"""
        t = self._now()
        if self._mode == "recording" and kind == "warn":
            self._notice_text = msg
            self._motion.show_notice(t)
            self._kick()
            return
        self._enter("toast", t, TOAST_HEX.get(kind, theme.STATE_OK_HEX))
        self._msg = self._capsule_text = msg
        self._motion.border.set(theme.qcolor(theme.CAPSULE_BORDER_HEX), t)
        self._relayout(t)
        self._hold(t, duration_ms)
        self._present()

    def show_delivery_confirmation(
        self,
        job_id: str,
        text: str,
        on_decision: Callable[..., None],
    ) -> None:
        """可点击的 `delivery_unknown` 重发确认卡片（只有卡片区域接收点击，仍不抢键盘焦点）。"""
        t = self._now()
        self._enter("confirm", t, theme.STATE_WARN_HEX)
        self._confirm = (job_id, on_decision)
        preview = normalize_single_line(text).strip()
        if len(preview) > CONFIRM_PREVIEW_CHARS:
            preview = f"{preview[:CONFIRM_PREVIEW_CHARS]}…"
        self._msg = self._capsule_text = (
            f"未确认是否已输入「{preview}」，是否重发？" if preview else "未确认文字是否已输入，是否重发？"
        )
        self._motion.border.set(view.with_alpha(theme.STATE_WARN_HEX, theme.VOICE_SOFT_BORDER_ALPHA), t)
        self._relayout(t)
        if not self._interactive:
            self._apply_interactivity(True)
        self._update_input_mask(t)
        self._present()

    def update_caption(self, stable_text: str = "", tail_text: str = "") -> None:
        """录音 / 润色中更新流式字幕；其它状态下忽略。"""
        if self._mode not in ("recording", "polishing"):
            return
        t = self._now()
        stable = normalize_single_line(stable_text)
        if len(stable) > len(self._stable):
            self._motion.mark_fresh(len(self._stable), len(stable), t)
        self._stable, self._tail = stable, normalize_single_line(tail_text)
        self._relayout(t)
        self._kick()

    def set_partial(self, stable_text: str = "", tail_text: str = "") -> None:
        self.update_caption(stable_text=stable_text, tail_text=tail_text)

    def finish_caption(self, final_text: str | None = None, hold_ms: int = CAPTION_AUTO_HIDE_MS) -> None:
        """定格字幕（给定最终文本或把尾巴并入已确认），hold_ms 后淡出。"""
        t = self._now()
        if final_text is not None:
            self._stable, self._tail = normalize_single_line(final_text), ""
        else:
            self._stable, self._tail = self._stable + self._tail, ""
        if self._mode != "idle":
            self._relayout(t)
        self._hold(t, max(1, int(hold_ms)))
        self._kick()

    def set_typed(self, text: str = "", hold_ms: int = CAPTION_AUTO_HIDE_MS) -> None:
        """已输入：旧字淡出、最终文字淡入，✓ 已输入，保持 hold_ms 后淡出。"""
        t = self._now()
        old_text = self._stable + self._tail
        old_spans = tuple(self._spans[-2:]) if self._form == "bar" else ()
        final = normalize_single_line(text) if text else old_text
        self._enter("toast", t, theme.STATE_OK_HEX)
        self._typed = True
        self._swap = (old_text, old_spans) if old_spans else None
        self._stable, self._tail = final, ""
        self._spans = []
        self._motion.mark_fresh(0, 0, t)
        self._motion.swap(t)
        self._capsule_text = TYPED_TEXT
        self._motion.border.set(view.with_alpha(theme.STATE_OK_HEX, theme.VOICE_FIRM_BORDER_ALPHA), t)
        self._relayout(t)
        self._hold(t, max(1, int(hold_ms)))
        self._present()

    def clear_caption(self) -> None:
        """立即清空字幕；已输入状态下随之隐藏。"""
        self._stable = self._tail = ""
        self._spans = []
        self._swap = None
        if self._typed:
            self._finish_hide()
            return
        if self._mode != "idle":
            self._relayout(self._now())
            self._kick()

    def visible_caption_lines(self) -> list[tuple[str, str]]:
        """当前字幕条实际显示的末尾行 (stable_seg, tail_seg)，长度恒 <= 2。"""
        if self._form != "bar":
            return []
        return compute_trailing_two_lines(self._stable, self._tail, view.INNER_MAX_W, self._fonts.caption)

    # ------------------------------------------------------------------
    # 状态切换
    # ------------------------------------------------------------------
    def _now(self) -> float:
        return self._clock() * 1000.0

    def _enter(self, mode: str, t: float, dot_hex: str) -> None:
        if self._interactive and mode != "confirm":
            self._apply_interactivity(False)
        if self._mode == "idle" or self._motion.out_at is not None:
            self._motion.enter(t)
        self._mode = mode
        self._typed = False
        self._hold_until = None
        if mode != "confirm":
            self._confirm = None
        self._motion.dot.set(theme.qcolor(dot_hex, opaque=True), t)

    def _hold(self, t: float, duration_ms: int) -> None:
        self._hold_until = t + duration_ms if duration_ms > 0 else None

    def _relayout(self, t: float) -> None:
        text = self._stable + self._tail
        if self._mode == "confirm":
            form = "card"
            width, height = view.card_size(self._msg, self._fonts)
        elif (self._mode in ("recording", "polishing") or self._typed) and text.strip():
            form = "bar"
            spans = view.caption_spans(text, self._fonts)
            if self._form == "bar" and not self._typed and len(spans) > len(self._spans) and len(spans) >= 3:
                self._motion.roll(t)
            self._spans = spans
            width, height = view.bar_size(text, spans, self._fonts)
        else:
            form = "pill"
            width, height = view.pill_size(self._sizing_label(), self._fonts)
        if form != self._form:
            self._form = form
            self._motion.change_form(t)
        bottom = self._bar_bottom()
        self._motion.retarget(QRectF((view.BAR_MAX_W - width) / 2, bottom - height, width, height), t)

    def _sizing_label(self) -> str:
        if self._mode == "recording":
            return RECORDING_SIZING_LABEL
        return TYPED_LABEL if self._typed else self._msg

    def _begin_exit(self, t: float) -> None:
        if self._mode == "idle":
            return
        self._hold_until = None
        self._confirm = None
        if self._interactive:
            self._apply_interactivity(False)
        self._motion.leave(t)
        self._kick()

    def _finish_hide(self) -> None:
        self._ticker.stop()
        if self._interactive:
            self._apply_interactivity(False)
        self._reset_state()
        self._motion.reset()
        self.hide()

    # ------------------------------------------------------------------
    # 窗口、帧驱动与绘制
    # ------------------------------------------------------------------
    def _bar_bottom(self) -> float:
        return theme.VOICE_NOTICE_HEIGHT_PX + self._fonts.bar_max_height()

    def _place_window(self) -> None:
        avail = theme.resolve_available_screen_rect(self._available_screen_override)
        width = int(view.BAR_MAX_W)
        height = int(self._bar_bottom() + theme.VOICE_RISE_PX)
        x = avail.x() + (avail.width() - width) // 2
        y = avail.y() + avail.height() - HUD_BOTTOM_MARGIN_PX - int(self._bar_bottom())
        target = QRect(x, y, width, height)
        if self.geometry() != target:
            self.setGeometry(target)

    def _present(self) -> None:
        if not self._enabled:
            self._kick()
            return
        self._place_window()
        if not self.isVisible():
            self.show()
        self._kick()

    def _kick(self) -> None:
        self.update()
        self._ticker.schedule(theme.VOICE_FRAME_MS)

    def _on_frame(self) -> None:
        t = self._now()
        if self._hold_until is not None and t >= self._hold_until:
            self._begin_exit(t)
        if self._motion.left(t):
            self._finish_hide()
            return
        if self._notice_text and self._motion.notice_done(t):
            self._notice_text = ""
        self._on_record_tick()
        self._update_input_mask(t)
        self.update()
        self._ticker.schedule(self._next_wake(t))

    def _next_wake(self, t: float) -> float | None:
        if self._mode == "idle":
            return None
        continuous = self._mode in ("recording", "polishing")
        if self.isVisible() and (continuous or self._motion.animating(t)):
            return theme.VOICE_FRAME_MS
        deadlines = [d for d in (self._hold_until, self._motion.notice_deadline(t)) if d is not None and d > t]
        if self._motion.out_at is not None:
            deadlines.append(self._motion.out_at + theme.VOICE_FADE_OUT_MS)
        return min(deadlines) - t if deadlines else None

    def _on_record_tick(self) -> None:
        """刷新录音秒表文案（只改文字，不改窗口几何）。"""
        if self._mode == "recording":
            self._capsule_text = self._pill_label(self._now())

    def _pill_label(self, t: float) -> str:
        if self._mode == "recording":
            return f"录音中 {self._status_text(t)}"
        return TYPED_LABEL if self._typed else self._msg

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        mins, secs = divmod(total, 60)
        return f"{mins}:{secs:02d}"

    def _frame(self, t: float) -> view.BarFrame | None:
        rect = self._motion.rect(t)
        if self._mode == "idle" or rect is None or self._form is None:
            return None
        border = self._motion.border.at(t)
        if self._mode == "polishing":
            border.setAlpha(theme.VOICE_BREATH_MIN_ALPHA + round(theme.VOICE_BREATH_SPAN_ALPHA * breathe(t, theme.VOICE_PULSE_PERIOD_MS)))
        icon_opacity = 1.0
        if self._mode == "recording":
            low = theme.VOICE_PULSE_MIN_OPACITY
            icon_opacity = low + (1.0 - low) * breathe(t, theme.VOICE_PULSE_PERIOD_MS)
        return view.BarFrame(
            form=self._form,
            rect=rect.translated(0.0, self._motion.offset_y(t)),
            opacity=self._motion.opacity(t),
            content_opacity=self._motion.content_opacity(t),
            border=border,
            icon="check" if self._typed else ("spin" if self._mode == "polishing" else "dot"),
            icon_color=self._motion.dot.at(t),
            icon_opacity=icon_opacity,
            spin_deg=spin_degrees(t, theme.VOICE_SPIN_PERIOD_MS),
            check=self._motion.check_progress(t),
            status_text=self._status_text(t),
            status_mono=self._mode == "recording",
            label=self._pill_label(t),
            caption=self._caption_view(t) if self._form == "bar" else None,
            notice=self._notice_view(t),
            hover=self._hover,
        )

    def _status_text(self, t: float) -> str:
        if self._mode == "recording":
            return self._format_duration((t - self._record_started_at) / 1000.0)
        return TYPED_LABEL if self._typed else "润色"

    def _caption_view(self, t: float) -> view.CaptionView:
        phase = self._motion.swap_phase(t) if self._swap is not None else None
        if phase is not None and phase[0] and self._swap is not None:
            old_text, old_spans = self._swap
            return view.CaptionView(old_text, old_spans, len(old_text), (0, 0), 1.0, 1.0, phase[1])
        return view.CaptionView(
            text=self._stable + self._tail,
            spans=tuple(self._spans),
            stable_len=len(self._stable),
            fresh=self._motion.fresh,
            fresh_mix=self._motion.fresh_mix(t),
            roll=self._motion.roll_progress(t),
            opacity=phase[1] if phase is not None else 1.0,
        )

    def _notice_view(self, t: float) -> view.NoticeView | None:
        state = self._motion.notice_view(t) if self._notice_text else None
        return None if state is None else view.NoticeView(self._notice_text, state[0], state[1])

    def paintEvent(self, _event: QPaintEvent) -> None:
        frame = self._frame(self._now())
        if frame is None:
            return
        painter = QPainter(self)
        view.paint_frame(painter, frame, self._fonts)
        painter.end()

    # ------------------------------------------------------------------
    # 确认卡片：输入区域、命中与回调
    # ------------------------------------------------------------------
    def _apply_interactivity(self, interactive: bool) -> None:
        was_visible = self.isVisible()
        self._interactive = interactive
        self.setWindowFlags(INTERACTIVE_NO_FOCUS_FLAGS if interactive else passive_overlay_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not interactive)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(interactive)
        if not interactive:
            self.clearMask()
            self._hover = None
        if was_visible and self._enabled:
            self.show()

    def _update_input_mask(self, t: float) -> None:
        """卡片模式下窗口输入区域 = 卡片（出现动画期间含下方上移余量）。"""
        target = self._motion.target()
        if not self._interactive or target is None:
            return
        extra = theme.VOICE_RISE_PX if self._motion.entering(t) else 0
        region = QRegion(target.toAlignedRect().adjusted(0, 0, 0, extra))
        if self.mask() != region:
            self.setMask(region)

    def _hit(self, pos: QPointF) -> str | None:
        frame = self._frame(self._now())
        if frame is None or frame.form != "card" or self._motion.out_at is not None:
            return None
        for key, box in view.card_buttons(frame.rect, self._fonts).items():
            if box.contains(pos):
                return key
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hover = self._hit(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        hit = self._hit(event.position())
        if hit is None:
            super().mousePressEvent(event)
            return
        event.accept()
        self._resolve_confirmation(hit == view.RESEND)

    def _resolve_confirmation(self, confirmed: bool) -> None:
        if self._confirm is None:
            return
        job_id, callback = self._confirm
        self._begin_exit(self._now())
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
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if len(positional) >= 2 and not has_varargs:
            callback(job_id, confirmed)
        else:
            callback(confirmed)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._ticker.stop()
        super().closeEvent(event)
