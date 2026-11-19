"""Sidecar 流式会话管理：单会话推理互斥锁、满队列溢出标志与终态事件必达淘汰。"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from _contracts import (
    ErrorCode,
    LogicError,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionSSEEvent,
)
from runtimes.asr.engine import AsrEngine
from runtimes.asr.scheduler import EngineScheduler

Clock = Callable[[], float]
_TERMINAL_KINDS = frozenset({"final", "error", "closed"})


def _is_terminal(event: Any) -> bool:
    if isinstance(event, (SessionFinalEvent, SessionErrorEvent, SessionClosedEvent)):
        return True
    kind = event.get("event") if isinstance(event, dict) else getattr(event, "event", None)
    return kind in _TERMINAL_KINDS


class SubscriberOverflowError(Exception):
    """SSE 订阅队列因慢消费发生 partial 溢出，供发送端显式推送错误并断开。"""


class SidecarSubscriber:
    """单个 SSE 连接的有界队列封装。

    - 普通 partial 满队列时置 `overflow = True`（修复向已满队列再 put 错误事件被静默吞掉的缺陷）；
    - 终态事件（final / error / closed）到达时若队列已满，优先淘汰队列中积压的旧 partial，确保终态必达。
    """

    def __init__(self, maxsize: int = 32) -> None:
        if maxsize < 1:
            raise LogicError(ErrorCode.invalid_request, "sse_queue_max 必须大于等于 1")
        self.maxsize = maxsize
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self.overflow = False
        self._terminal_tail: deque[Any] = deque()
        self._lock = threading.Lock()

    def publish(self, event: Any) -> None:
        terminal = _is_terminal(event)
        with self._lock:
            if not terminal:
                try:
                    self.queue.put_nowait(event)
                except queue.Full:
                    self.overflow = True
                return

            # 终态事件必达：若队列已满，淘汰旧 partial 腾出空间；保留已有终态事件的严格顺序
            if self.queue.full():
                kept: list[Any] = []
                evicted_one = False
                while True:
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if not evicted_one and not _is_terminal(item):
                        evicted_one = True
                        continue
                    kept.append(item)
                for item in kept:
                    try:
                        self.queue.put_nowait(item)
                    except queue.Full:
                        self._terminal_tail.append(item)

            try:
                self.queue.put_nowait(event)
            except queue.Full:
                self._terminal_tail.append(event)
            # 终态已接管会话最终结果，不再因早期过时 partial 溢出截断终态投递
            self.overflow = False

    def poll(self, timeout_s: float = 0.2) -> Any | None:
        with self._lock:
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                if self._terminal_tail:
                    return self._terminal_tail.popleft()
                if self.overflow:
                    self.overflow = False
                    raise SubscriberOverflowError()

        try:
            return self.queue.get(True, timeout_s)
        except queue.Empty:
            with self._lock:
                if self._terminal_tail:
                    return self._terminal_tail.popleft()
                if self.overflow:
                    self.overflow = False
                    raise SubscriberOverflowError()
            return None


class HubSession:
    """单个活动流式会话状态，自带 infer_lock 保证同一会话底层解码步严格串行。"""

    def __init__(self, session_id: str, now: float) -> None:
        self.session_id = session_id
        self.created_at = now
        self.last_activity = now
        self.state = "open"
        self.subscribers: list[SidecarSubscriber] = []
        self.infer_lock = threading.RLock()


class SidecarSessionManager:
    """线程安全的会话表、SSE 扇出与空闲超时清理。"""

    def __init__(
        self,
        *,
        engine: AsrEngine,
        scheduler: EngineScheduler,
        clock: Clock = time.monotonic,
        idle_s: float = 180.0,
        sse_queue_max: int = 32,
    ) -> None:
        if sse_queue_max < 1:
            raise LogicError(ErrorCode.invalid_request, "sse_queue_max 必须大于等于 1")
        self.engine = engine
        self.scheduler = scheduler
        self.clock = clock
        self.idle_s = idle_s
        self.sse_queue_max = sse_queue_max
        self.sessions: dict[str, HubSession] = {}
        self._guard = threading.RLock()

    def register_open(self, session_id: str) -> HubSession:
        with self._guard:
            session = HubSession(session_id, self.clock())
            self.sessions[session_id] = session
            return session

    def get_open(self, session_id: str) -> HubSession:
        with self._guard:
            session = self.sessions.get(session_id)
            if session is None or session.state != "open":
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            return session

    def touch(self, session: HubSession) -> None:
        with self._guard:
            session.last_activity = self.clock()

    def subscribe(self, session: HubSession) -> SidecarSubscriber:
        sub = SidecarSubscriber(maxsize=self.sse_queue_max)
        with self._guard:
            session.subscribers.append(sub)
        return sub

    def unsubscribe(self, session: HubSession, sub: SidecarSubscriber) -> None:
        with self._guard:
            session.subscribers = [item for item in session.subscribers if item is not sub]

    def publish(self, session: HubSession, event: SessionSSEEvent | dict[str, Any]) -> None:
        with self._guard:
            targets = list(session.subscribers)
        for sub in targets:
            sub.publish(event)

    def close_session(
        self,
        session: HubSession,
        reason: SessionClosedReason,
        error: SessionErrorEvent | None = None,
    ) -> None:
        with self._guard:
            already_closed = session.state != "open"
            if not already_closed:
                session.state = reason.value
            self.sessions.pop(session.session_id, None)

        if not already_closed:
            if error is not None:
                self.publish(session, error)
            self.publish(
                session,
                SessionClosedEvent(session_id=session.session_id, reason=reason),
            )

        with session.infer_lock:
            if self.engine.has_stream(session.session_id):
                try:
                    self.engine.cancel_stream(session.session_id)
                except LogicError:
                    pass
                except Exception:
                    pass

        self.scheduler.release_stream(session.session_id)

    def sweep(self) -> list[str]:
        now = self.clock()
        with self._guard:
            stale = [
                session
                for session in self.sessions.values()
                if session.state == "open" and now - session.last_activity >= self.idle_s
            ]
        closed_ids: list[str] = []
        for session in stale:
            self.close_session(
                session,
                SessionClosedReason.disconnected,
                SessionErrorEvent(
                    session_id=session.session_id,
                    error=ErrorCode.timeout,
                    message="会话空闲超时，已释放引擎与音频缓存",
                ),
            )
            closed_ids.append(session.session_id)
        return closed_ids


__all__ = [
    "Clock",
    "HubSession",
    "SidecarSessionManager",
    "SidecarSubscriber",
    "SubscriberOverflowError",
]
