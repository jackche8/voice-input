"""快捷键单一按住状态：一次按下 = evdev 报告的按下区间 [s, e]，GNOME `Activated` 只负责放行。

纯逻辑（无 GLib、无 IO、无锁），只在 Qt 主线程调用；输入时间戳均为单调毫秒。门户时间戳来自
Clutter 的 32 位事件时间（约 49.7 天回绕），所以一律按 32 位回绕比较。
规则见 docs/plans/v3.3-overlay-voice-hotkey-plan.md 8.6-F 与 8.18。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal["voice", "screenshot", "ocr", "repeat", "annotate"]
EdgeKind = Literal["voice_start", "voice_stop", "fire"]
ACTIONS: tuple[Action, ...] = ("voice", "screenshot", "ocr", "repeat", "annotate")

_MASK = 0xFFFFFFFF
_HALF = 0x80000000


@dataclass(frozen=True)
class Edge:
    """状态机要求执行的一个动作边沿。"""

    kind: EdgeKind
    action: Action


def ts_le(a_ms: int, b_ms: int) -> bool:
    """32 位回绕毫秒：a 不晚于 b。"""
    return ((b_ms - a_ms) & _MASK) < _HALF


def _ts_lt(a_ms: int, b_ms: int) -> bool:
    return (a_ms & _MASK) != (b_ms & _MASK) and ts_le(a_ms, b_ms)


@dataclass
class _Interval:
    start: int
    end: int | None = None
    triggered: bool = False

    def contains(self, t: int) -> bool:
        return ts_le(self.start, t) and (self.end is None or ts_le(t, self.end))


@dataclass
class _Combo:
    # 最近一个已触发的区间及其后的全部区间（尚无已触发时为全部），按时间顺序
    intervals: list[_Interval] = field(default_factory=list)
    # 还不能判定的放行时间戳，按时间顺序
    pending: list[int] = field(default_factory=list)

    def find(self, t: int) -> _Interval | None:
        for interval in self.intervals:
            if interval.contains(t):
                return interval
        return None


class HotkeyPressState:
    """五个组合共用的唯一按住状态；每个输入返回需要执行的边沿。"""

    def __init__(self) -> None:
        self._combos: dict[Action, _Combo] = {action: _Combo() for action in ACTIONS}
        self._voice_active = False

    @property
    def voice_active(self) -> bool:
        return self._voice_active

    def interval_started(self, action: Action, s_ms: int) -> tuple[Edge, ...]:
        combo = self._combos[action]
        if combo.intervals and combo.intervals[-1].end is None:
            return ()
        combo.intervals.append(_Interval(start=s_ms & _MASK))
        return self._match_pending(action, combo)

    def interval_ended(self, action: Action, e_ms: int) -> tuple[Edge, ...]:
        combo = self._combos[action]
        if not combo.intervals or combo.intervals[-1].end is not None:
            return ()
        latest = combo.intervals[-1]
        latest.end = e_ms & _MASK
        edges: list[Edge] = []
        if latest.triggered and action == "voice" and self._voice_active:
            self._voice_active = False
            edges.append(Edge("voice_stop", action))
        edges.extend(self._match_pending(action, combo))
        return tuple(edges)

    def activated(self, action: Action, t_ms: int) -> tuple[Edge, ...]:
        combo = self._combos[action]
        t = t_ms & _MASK
        target = combo.find(t)
        if target is not None and not target.triggered:
            edges = self._trigger(action, combo, target)
        elif target is not None and target.end is not None:
            edges = ()  # 已触发且已结束：松手前发出的连发
        else:
            self._insert_pending(combo, t)
            edges = ()
        # 同一组合的 Activated 按时间顺序到达：结束早于 t 的未触发区间不会再被放行
        combo.intervals = [
            iv for iv in combo.intervals if iv.triggered or iv.end is None or not _ts_lt(iv.end, t)
        ]
        return edges

    def reader_progress(self, p_ms: int) -> None:
        """evdev 读线程已处理到 p（与区间同一队列、在其后投递）：早于 p 且不在任何已记录区间内的挂起放行
        不会再配上区间（evdev 看不到的输入来源），丢弃；落在已触发未结束区间内的挂起保留（8.16 R3）。"""
        p = p_ms & _MASK
        for combo in self._combos.values():
            combo.pending = [t for t in combo.pending if not _ts_lt(t, p) or combo.find(t) is not None]

    def start_failed(self) -> None:
        """voice_start 失败：录音不在进行，区间仍记已触发（同一次按住的连发不重试）。"""
        self._voice_active = False

    def reset(self, action: Action) -> None:
        self._combos[action] = _Combo()
        if action == "voice":
            self._voice_active = False

    def _insert_pending(self, combo: _Combo, t: int) -> None:
        index = len(combo.pending)
        while index > 0 and _ts_lt(t, combo.pending[index - 1]):
            index -= 1
        combo.pending.insert(index, t)

    def _match_pending(self, action: Action, combo: _Combo) -> tuple[Edge, ...]:
        edges: list[Edge] = []
        kept: list[int] = []
        for t in combo.pending:
            target = combo.find(t)
            if target is not None and not target.triggered:
                edges.extend(self._trigger(action, combo, target))
                continue
            if target is not None:
                if target.end is None:
                    kept.append(t)  # 已触发未结束：可能是连发，也可能是还没上报的下一次按下
                continue
            latest = combo.intervals[-1] if combo.intervals else None
            if latest is not None and _ts_lt(t, latest.start):
                continue  # 早于最新区间且不在任何区间内：不会再有区间包含它
            kept.append(t)
        combo.pending = kept
        return tuple(edges)

    def _trigger(self, action: Action, combo: _Combo, target: _Interval) -> list[Edge]:
        target.triggered = True
        del combo.intervals[: combo.intervals.index(target)]
        if action != "voice":
            return [Edge("fire", action)]
        edges = [Edge("voice_start", action)]
        self._voice_active = True
        if target.end is not None:
            self._voice_active = False
            edges.append(Edge("voice_stop", action))
        return edges


__all__ = ["ACTIONS", "Action", "Edge", "EdgeKind", "HotkeyPressState", "ts_le"]
