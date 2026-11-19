"""最简实时优先调度：同一时刻只有一个推理占用，槽位原子移交零饥饿竞态。

流式优先于等待中的整段转写；不抢占已开始的推理。
release() 在同一把互斥锁内把槽位所有权直接移交给队首有效等待者，
杜绝「先置 None 再唤醒导致新请求插队抢占、等待者醒来抛 409 conflict 或槽位泄露」的竞态。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from _contracts import ErrorCode, LogicError

Kind = Literal["stream", "batch"]


@dataclass
class _Waiter:
    kind: Kind
    session_id: str | None
    event: threading.Event
    granted: bool = False
    cancelled: bool = False


class EngineScheduler:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: Kind | None = None
        self._active_stream_id: str | None = None
        self._waiters: deque[_Waiter] = deque()

    @property
    def active_stream_id(self) -> str | None:
        with self._guard:
            return self._active_stream_id

    def _has_pending_stream_unlocked(self) -> bool:
        return any(w.kind == "stream" and not w.cancelled for w in self._waiters)

    def acquire_stream(self, session_id: str, *, timeout_s: float = 120.0) -> None:
        with self._guard:
            if (
                self._active == "stream"
                or self._active_stream_id is not None
                or self._has_pending_stream_unlocked()
            ):
                raise LogicError(ErrorCode.conflict, "已有识别会话在进行，sidecar 只支持单会话")
            if self._active is None:
                self._active = "stream"
                self._active_stream_id = session_id
                return
            waiter = _Waiter(kind="stream", session_id=session_id, event=threading.Event())
            self._waiters.appendleft(waiter)

        waiter.event.wait(timeout=timeout_s)
        with self._guard:
            if waiter.granted:
                return
            waiter.cancelled = True
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass
            raise LogicError(ErrorCode.timeout, "等待让出官方引擎超时")

    def acquire_batch(self, *, timeout_s: float = 120.0) -> None:
        with self._guard:
            if self._active is None and not self._waiters:
                self._active = "batch"
                self._active_stream_id = None
                return
            waiter = _Waiter(kind="batch", session_id=None, event=threading.Event())
            self._waiters.append(waiter)

        waiter.event.wait(timeout=timeout_s)
        with self._guard:
            if waiter.granted:
                return
            waiter.cancelled = True
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass
            raise LogicError(ErrorCode.timeout, "等待整段转写槽位超时")

    def _handoff_unlocked(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled:
                continue
            self._active = waiter.kind
            self._active_stream_id = (
                waiter.session_id if waiter.kind == "stream" else None
            )
            waiter.granted = True
            waiter.event.set()
            return
        self._active = None
        self._active_stream_id = None

    def release_stream(self, session_id: str) -> bool:
        """原子释放指定流式会话占用的槽位并移交队首等待者；非当前持有者则 no-op 返回 False。"""
        with self._guard:
            if self._active != "stream" or self._active_stream_id != session_id:
                return False
            self._handoff_unlocked()
            return True

    def release_batch(self) -> bool:
        """原子释放整段转写槽位并移交队首等待者；当前非 batch 占槽则 no-op 返回 False。"""
        with self._guard:
            if self._active != "batch":
                return False
            self._handoff_unlocked()
            return True

    def release(self) -> None:
        with self._guard:
            self._handoff_unlocked()

    def hold_stream(self, session_id: str) -> Any:
        return _Held(self, "stream", session_id)

    def hold_batch(self) -> Any:
        return _Held(self, "batch", None)


class _Held:
    def __init__(self, sched: EngineScheduler, kind: Kind, session_id: str | None) -> None:
        self._sched = sched
        self._kind = kind
        self._session_id = session_id

    def __enter__(self) -> EngineScheduler:
        if self._kind == "stream":
            assert self._session_id is not None
            self._sched.acquire_stream(self._session_id)
        else:
            self._sched.acquire_batch()
        return self._sched

    def __exit__(self, *exc: object) -> None:
        if self._kind == "batch":
            self._sched.release_batch()


__all__ = ["EngineScheduler", "Kind"]
