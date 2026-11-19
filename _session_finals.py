"""桌面会话 final 的业务侧索引。只记录已定稿结果，不实现 sidecar 流式，也不拼接长音频。

一次绑定只是进程内提示；崩溃后以已持久 JobRecord.session_id 为准，不另建绑定库。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from _contracts import ErrorCode, LogicError, SessionClosedReason

MAX_SESSION_FINALS = 512


@dataclass(frozen=True)
class SessionFinalSnapshot:
    session_id: str
    text: str = ""
    language: str | None = None
    has_final: bool = False
    closed_reason: SessionClosedReason | None = None
    bound_job_id: str | None = None


class SessionFinalIndex:
    """平台或测试在收到会话 final/closed 后登记，供 utterance 查阅。

    线程安全且限制最大 512 条记录（FIFO 淘汰最早会话），防止常驻服务内存无界增长。
    bound_job_id 必须与 jobs 中的有效引用一致，不能代替持久化 JobRecord.session_id。
    """

    def __init__(self, *, max_entries: int = MAX_SESSION_FINALS) -> None:
        if max_entries < 1:
            raise LogicError(ErrorCode.invalid_request, "max_entries 必须大于等于 1")
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._items: dict[str, SessionFinalSnapshot] = {}

    def _put_unlocked(self, session_id: str, snap: SessionFinalSnapshot) -> None:
        if session_id not in self._items:
            while len(self._items) >= self._max_entries:
                oldest_key = next(iter(self._items))
                self._items.pop(oldest_key, None)
        self._items[session_id] = snap

    def record_final(
        self, session_id: str, text: str, language: str | None = None
    ) -> SessionFinalSnapshot:
        if not session_id:
            raise LogicError(ErrorCode.invalid_request, "session_id 不能为空")
        with self._lock:
            current = self._items.get(session_id)
            snap = SessionFinalSnapshot(
                session_id=session_id,
                text=text,
                language=language,
                has_final=True,
                closed_reason=current.closed_reason if current is not None else None,
                bound_job_id=current.bound_job_id if current is not None else None,
            )
            self._put_unlocked(session_id, snap)
            return snap

    def record_closed(
        self, session_id: str, reason: SessionClosedReason
    ) -> SessionFinalSnapshot:
        if not session_id:
            raise LogicError(ErrorCode.invalid_request, "session_id 不能为空")
        with self._lock:
            current = self._items.get(session_id)
            snap = SessionFinalSnapshot(
                session_id=session_id,
                text=current.text if current is not None else "",
                language=current.language if current is not None else None,
                has_final=current.has_final if current is not None else False,
                closed_reason=reason,
                bound_job_id=current.bound_job_id if current is not None else None,
            )
            self._put_unlocked(session_id, snap)
            return snap

    def bind_job(self, session_id: str, job_id: str) -> SessionFinalSnapshot:
        if not session_id or not job_id:
            raise LogicError(ErrorCode.invalid_request, "session 绑定需要 session_id 与 job_id")
        with self._lock:
            current = self._items.get(session_id)
            if current is None or not current.has_final:
                raise LogicError(ErrorCode.invalid_request, "无 final，不能绑定 job")
            if current.bound_job_id and current.bound_job_id != job_id:
                raise LogicError(
                    ErrorCode.conflict,
                    "该 session 已绑定其他 job_id，不能换 id 绕过",
                    details={
                        "session_id": session_id,
                        "bound_job_id": current.bound_job_id,
                        "job_id": job_id,
                    },
                )
            snap = SessionFinalSnapshot(
                session_id=current.session_id,
                text=current.text,
                language=current.language,
                has_final=current.has_final,
                closed_reason=current.closed_reason,
                bound_job_id=job_id,
            )
            self._items[session_id] = snap
            return snap

    def lookup(self, session_id: str) -> SessionFinalSnapshot | None:
        with self._lock:
            return self._items.get(session_id)


__all__ = ["MAX_SESSION_FINALS", "SessionFinalIndex", "SessionFinalSnapshot"]
