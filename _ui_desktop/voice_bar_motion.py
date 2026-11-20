"""语音一体底栏动效（UI 验收标准 docs/design/v3.3/voice-a.html「动效参数」表）。

单一职责：记录各动画的开始时刻与起止值，按调用方给出的时刻（毫秒）换算当前进度与插值；
`FrameTicker` 有动画时按帧唤醒重绘，只剩定时节点时按剩余时间唤醒，空闲即停。
不含绘制、业务状态与时钟读取。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QObject, QRectF, QTimer
from PySide6.QtGui import QColor

from _ui_desktop import theme

NEVER = float("-inf")
_OUT_CUBIC = QEasingCurve(QEasingCurve.Type.OutCubic)
_IN_OUT_SINE = QEasingCurve(QEasingCurve.Type.InOutSine)


def linear(t_ms: float, start_ms: float, duration_ms: float) -> float:
    """线性进度 0..1；从未开始的动画（start=NEVER）视为已完成。"""
    if duration_ms <= 0 or t_ms >= start_ms + duration_ms:
        return 1.0
    if t_ms <= start_ms:
        return 0.0
    return (t_ms - start_ms) / duration_ms


def ease_out(t_ms: float, start_ms: float, duration_ms: float) -> float:
    """OutCubic 缓动进度 0..1。"""
    return _OUT_CUBIC.valueForProgress(linear(t_ms, start_ms, duration_ms))


def breathe(t_ms: float, period_ms: float) -> float:
    """周期呼吸 1 → 0 → 1（余弦形），用于录音点与润色描边。"""
    phase = (t_ms % period_ms) / period_ms
    half = phase * 2.0 if phase < 0.5 else 2.0 - phase * 2.0
    return 1.0 - _IN_OUT_SINE.valueForProgress(half)


def spin_degrees(t_ms: float, period_ms: float) -> float:
    """匀速旋转角度（度），用于润色旋转弧。"""
    return (t_ms % period_ms) / period_ms * 360.0


def mix(a: QColor, b: QColor, p: float) -> QColor:
    """两色按进度线性插值（含 alpha）。"""
    return QColor(
        round(a.red() + (b.red() - a.red()) * p),
        round(a.green() + (b.green() - a.green()) * p),
        round(a.blue() + (b.blue() - a.blue()) * p),
        round(a.alpha() + (b.alpha() - a.alpha()) * p),
    )


def _lerp_rect(a: QRectF, b: QRectF, p: float) -> QRectF:
    return QRectF(
        a.x() + (b.x() - a.x()) * p,
        a.y() + (b.y() - a.y()) * p,
        a.width() + (b.width() - a.width()) * p,
        a.height() + (b.height() - a.height()) * p,
    )


class _ColorTween:
    """单个颜色过渡：首次设置直接到位，之后从当前插值色过渡到新色。"""

    def __init__(self) -> None:
        self._from = QColor()
        self._to = QColor()
        self._at = NEVER

    def set(self, color: QColor, t_ms: float) -> None:
        self._from = self.at(t_ms) if self._to.isValid() else QColor(color)
        self._to = QColor(color)
        self._at = t_ms

    def at(self, t_ms: float) -> QColor:
        return mix(self._from, self._to, ease_out(t_ms, self._at, theme.VOICE_COLOR_MS))

    def busy(self, t_ms: float) -> bool:
        return t_ms < self._at + theme.VOICE_COLOR_MS


class BarMotion:
    """一体底栏全部动画的开始时刻（ms）与起止值；查询方法只读。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.in_at = NEVER
        self.out_at: float | None = None
        self._rect_from: QRectF | None = None
        self._rect_to: QRectF | None = None
        self._rect_at = NEVER
        self.form_at = NEVER
        self.roll_at = NEVER
        self.fresh: tuple[int, int] = (0, 0)
        self.fresh_at = NEVER
        self.swap_at = NEVER
        self.notice_at = NEVER
        self.dot = _ColorTween()
        self.border = _ColorTween()

    # --- 出现 / 退出 ---------------------------------------------------------
    def enter(self, t_ms: float) -> None:
        """重新出现：淡入 + 上移，下一次目标矩形直接到位。"""
        self.in_at = t_ms
        self.out_at = None
        self._rect_from = None
        self._rect_to = None

    def leave(self, t_ms: float) -> None:
        if self.out_at is None:
            self.out_at = t_ms

    def left(self, t_ms: float) -> bool:
        return self.out_at is not None and t_ms >= self.out_at + theme.VOICE_FADE_OUT_MS

    def entering(self, t_ms: float) -> bool:
        return t_ms < self.in_at + theme.VOICE_FADE_IN_MS

    def opacity(self, t_ms: float) -> float:
        fade_in = ease_out(t_ms, self.in_at, theme.VOICE_FADE_IN_MS)
        return fade_in * (1.0 - self._out_progress(t_ms))

    def offset_y(self, t_ms: float) -> float:
        fade_in = ease_out(t_ms, self.in_at, theme.VOICE_FADE_IN_MS)
        return (1.0 - fade_in) * theme.VOICE_RISE_PX + self._out_progress(t_ms) * theme.VOICE_SINK_PX

    def _out_progress(self, t_ms: float) -> float:
        return 0.0 if self.out_at is None else ease_out(t_ms, self.out_at, theme.VOICE_FADE_OUT_MS)

    # --- 形变 -----------------------------------------------------------------
    def retarget(self, rect: QRectF, t_ms: float) -> None:
        """目标矩形变化时从当前插值矩形起形变；首次直接到位。"""
        if self._rect_to is not None and self._rect_to == rect:
            return
        self._rect_from = self.rect(t_ms) if self._rect_to is not None else QRectF(rect)
        self._rect_to = QRectF(rect)
        self._rect_at = t_ms

    def rect(self, t_ms: float) -> QRectF | None:
        if self._rect_to is None or self._rect_from is None:
            return None
        return _lerp_rect(self._rect_from, self._rect_to, ease_out(t_ms, self._rect_at, theme.VOICE_MORPH_MS))

    def target(self) -> QRectF | None:
        return None if self._rect_to is None else QRectF(self._rect_to)

    def change_form(self, t_ms: float) -> None:
        self.form_at = t_ms

    def content_opacity(self, t_ms: float) -> float:
        return ease_out(t_ms, self.form_at + theme.VOICE_CONTENT_DELAY_MS, theme.VOICE_CONTENT_FADE_MS)

    # --- 字幕 -----------------------------------------------------------------
    def roll(self, t_ms: float) -> None:
        self.roll_at = t_ms

    def roll_progress(self, t_ms: float) -> float:
        return ease_out(t_ms, self.roll_at, theme.VOICE_ROLL_MS)

    def mark_fresh(self, start: int, end: int, t_ms: float) -> None:
        """记录刚由 tail 转为 stable 的字符区间，颜色在 VOICE_FRESH_MS 内过渡。"""
        self.fresh = (start, end)
        self.fresh_at = t_ms

    def fresh_mix(self, t_ms: float) -> float:
        return ease_out(t_ms, self.fresh_at, theme.VOICE_FRESH_MS)

    def swap(self, t_ms: float) -> None:
        """已输入：旧字淡出 → 最终文字淡入，同时 ✓ 描边出现。"""
        self.swap_at = t_ms

    def swap_phase(self, t_ms: float) -> tuple[bool, float] | None:
        """(是否仍显示旧字, 当前字幕透明度)；切换结束返回 None。"""
        half = theme.VOICE_SWAP_HALF_MS
        if t_ms >= self.swap_at + 2 * half:
            return None
        if t_ms < self.swap_at + half:
            return True, 1.0 - ease_out(t_ms, self.swap_at, half)
        return False, ease_out(t_ms, self.swap_at + half, half)

    def check_progress(self, t_ms: float) -> float:
        return ease_out(t_ms, self.swap_at, theme.VOICE_CHECK_MS)

    # --- 附加提示行 -------------------------------------------------------------
    def show_notice(self, t_ms: float) -> None:
        self.notice_at = t_ms

    def clear_notice(self) -> None:
        self.notice_at = NEVER

    def notice_view(self, t_ms: float) -> tuple[float, float] | None:
        """(透明度, 升起剩余位移)；未显示或已淡出完毕返回 None。"""
        hold_end = self.notice_at + theme.VOICE_NOTICE_SHOW_MS
        if t_ms < self.notice_at or t_ms >= hold_end + theme.VOICE_NOTICE_OUT_MS:
            return None
        fade_in = ease_out(t_ms, self.notice_at, theme.VOICE_NOTICE_IN_MS)
        fade_out = ease_out(t_ms, hold_end, theme.VOICE_NOTICE_OUT_MS) if t_ms > hold_end else 0.0
        return fade_in * (1.0 - fade_out), (1.0 - fade_in) * theme.VOICE_NOTICE_RISE_PX

    def notice_done(self, t_ms: float) -> bool:
        return t_ms >= self.notice_at + theme.VOICE_NOTICE_SHOW_MS + theme.VOICE_NOTICE_OUT_MS

    def notice_deadline(self, t_ms: float) -> float | None:
        """提示行保持期间的下一个时间节点（开始淡出），供空闲时按剩余时间唤醒。"""
        hold_end = self.notice_at + theme.VOICE_NOTICE_SHOW_MS
        return hold_end if self.notice_at <= t_ms < hold_end else None

    # --- 调度 -----------------------------------------------------------------
    def animating(self, t_ms: float) -> bool:
        """当前是否有任何过渡在进行（需要按帧重绘）。"""
        hold_end = self.notice_at + theme.VOICE_NOTICE_SHOW_MS
        return any((
            self.entering(t_ms),
            self.out_at is not None,
            t_ms < self._rect_at + theme.VOICE_MORPH_MS,
            t_ms < self.form_at + theme.VOICE_CONTENT_DELAY_MS + theme.VOICE_CONTENT_FADE_MS,
            t_ms < self.roll_at + theme.VOICE_ROLL_MS,
            t_ms < self.fresh_at + theme.VOICE_FRESH_MS,
            t_ms < self.swap_at + max(2 * theme.VOICE_SWAP_HALF_MS, theme.VOICE_CHECK_MS),
            self.dot.busy(t_ms),
            self.border.busy(t_ms),
            t_ms < self.notice_at + theme.VOICE_NOTICE_IN_MS,
            hold_end <= t_ms < hold_end + theme.VOICE_NOTICE_OUT_MS,
        ))


class FrameTicker(QObject):
    """单个单发 QTimer：每次唤醒后由调用方重新排程；不排程即停。"""

    def __init__(self, on_tick: Callable[[], None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(on_tick)

    def schedule(self, delay_ms: float | None) -> None:
        if delay_ms is None:
            self._timer.stop()
            return
        self._timer.start(max(1, -int(-delay_ms // 1)))

    def stop(self) -> None:
        self._timer.stop()
