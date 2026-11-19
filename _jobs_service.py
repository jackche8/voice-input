"""Job 队列的业务规则：事件唤醒 claim、幂等 ack、崩溃恢复与租约过期收敛，不重投。

存取只通过 JobStore。认证与来源判定不在本模块。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from _contracts import (
    AckResult,
    ErrorCode,
    JobAckRequest,
    JobClaimed,
    JobQueryFound,
    JobQueryUnknown,
    JobRecord,
    JobState,
    LogicError,
    ProductDeliveryUnknownEvent,
    ProductErrorEvent,
    ProductTypedEvent,
)
from _events import EventBus
from _jobs import JobStore

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _target_state_for_ack(result: AckResult) -> JobState:
    if result == AckResult.complete:
        return JobState.done
    if result == AckResult.none:
        return JobState.failed
    if result == AckResult.partial_or_unknown:
        return JobState.delivery_unknown
    raise LogicError(ErrorCode.invalid_request, f"未知 ack result: {result}")


class JobsService:
    def __init__(
        self,
        store: JobStore,
        events: EventBus,
        *,
        clock: Clock | None = None,
        claim_lease_s: float = 60.0,
    ) -> None:
        self._store = store
        self._events = events
        self._clock = clock or _utc_now
        self._claim_lease_s = claim_lease_s

    def _emit_delivery_unknown(self, record: JobRecord) -> None:
        self._events.publish(
            ProductDeliveryUnknownEvent(job_id=record.job_id, text=record.text)
        )

    def get(self, job_id: str) -> JobQueryFound | JobQueryUnknown:
        if not job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 不能为空")
        self.expire_stale_claims()
        record = self._store.get(job_id)
        if record is None:
            return JobQueryUnknown(job_id=job_id)
        return JobQueryFound(job=record)

    def enqueue(self, record: JobRecord) -> JobRecord:
        return self._store.enqueue(record)

    def find_by_session_id(self, session_id: str) -> JobRecord | None:
        """以已持久的 JobRecord.session_id 查证绑定，不另建索引库。"""
        if not session_id:
            return None
        matches = [job for job in self._store.list_jobs() if job.session_id == session_id]
        if not matches:
            return None
        if len(matches) > 1:
            raise LogicError(
                ErrorCode.conflict,
                "同一 session 存在多条 job 记录",
                details={"session_id": session_id, "job_ids": [job.job_id for job in matches]},
            )
        return matches[0]

    def claim_job(self, timeout_s: float | None = None) -> JobClaimed | None:
        """基于 JobStore.wait_and_claim_next 的零轮询事件唤醒领取。"""
        wait = max(0.0, float(timeout_s or 0.0))
        claimed = self._store.wait_and_claim_next(
            timeout_s=wait,
            stale_after_s=self._claim_lease_s,
            clock=self._clock,
            on_expired=self._emit_delivery_unknown,
        )
        if claimed is None:
            return None
        return JobClaimed(
            job_id=claimed.job_id,
            claim_id=claimed.claim_id or "",
            text=claimed.text,
            source=claimed.source,
            focus_id=claimed.focus_id,
        )

    def claim(
        self,
        wait_s: float | None = None,
        *,
        timeout_s: float | None = None,
    ) -> JobClaimed | None:
        """原子 queued→claimed。支持 Condition 事件唤醒等待，无任务返回 None。"""
        effective_wait = timeout_s if timeout_s is not None else wait_s
        return self.claim_job(timeout_s=effective_wait)

    def ack(self, job_id: str, request: JobAckRequest) -> JobRecord:
        if not job_id:
            raise LogicError(ErrorCode.invalid_request, "job_id 不能为空")
        if not request.claim_id:
            raise LogicError(ErrorCode.invalid_request, "claim_id 不能为空")
        target_state = _target_state_for_ack(request.result)
        current = self._store.get(job_id)
        if current is None:
            raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
        if current.claim_id != request.claim_id:
            raise LogicError(
                ErrorCode.conflict,
                "claim_id 不匹配",
                details={"job_id": job_id},
            )
        # 同一 (claim_id, 目标 state) 重复 ack（例如响应网络超时重试）幂等返回，不重复写盘与发事件
        if current.state == target_state:
            return current
        if current.state in (JobState.claimed, JobState.delivery_unknown):
            return self._apply_ack(current, request, target_state)
        raise LogicError(
            ErrorCode.conflict,
            f"当前状态不可 ack 为 {target_state}: {current.state}",
            details={"job_id": job_id, "state": current.state, "target_state": target_state},
        )

    def _apply_ack(
        self,
        current: JobRecord,
        request: JobAckRequest,
        new_state: JobState,
    ) -> JobRecord:
        now = _iso(self._clock())
        try:
            updated = self._store.update_if(
                current.job_id,
                expected={"state": current.state, "claim_id": current.claim_id},
                fields={
                    "state": new_state,
                    "updated_at": now,
                    "detail": request.detail,
                },
            )
        except LogicError as exc:
            if exc.code == ErrorCode.conflict:
                latest = self._store.get(current.job_id)
                if (
                    latest is not None
                    and latest.claim_id == request.claim_id
                    and latest.state == new_state
                ):
                    return latest
                raise LogicError(
                    ErrorCode.conflict,
                    "ack 条件不成立，可能已被其他调用收敛",
                    details={"job_id": current.job_id},
                ) from exc
            raise
        if updated.state == JobState.done:
            self._events.publish(ProductTypedEvent(job_id=updated.job_id, text=updated.text))
        elif updated.state == JobState.failed:
            self._events.publish(
                ProductErrorEvent(
                    error=ErrorCode.delivery_failed,
                    message=request.detail or "未输入",
                    details={"job_id": updated.job_id},
                )
            )
        elif updated.state == JobState.delivery_unknown:
            self._emit_delivery_unknown(updated)
        return updated

    def expire_stale_claims(self, *, now: datetime | None = None) -> list[JobRecord]:
        """claimed 超过租约 → delivery_unknown，不退回 queued，不重投。"""
        moment = now or self._clock()
        changed = self._store.expire_stale_claims(
            stale_after_s=self._claim_lease_s,
            moment=moment,
        )
        for updated in changed:
            self._emit_delivery_unknown(updated)
        return changed

    def recover(self) -> list[JobRecord]:
        """进程恢复：claimed 视为投递未知并记下，不把任何状态退回 queued，不从历史重建队列。"""
        changed: list[JobRecord] = []
        now = _iso(self._clock())
        for job in self._store.list_jobs():
            if job.state != JobState.claimed:
                continue
            try:
                updated = self._store.update_if(
                    job.job_id,
                    expected={"state": JobState.claimed, "claim_id": job.claim_id},
                    fields={
                        "state": JobState.delivery_unknown,
                        "updated_at": now,
                        "detail": "recovered after interruption; not redelivered",
                    },
                )
            except LogicError as exc:
                if exc.code == ErrorCode.conflict:
                    continue
                raise
            self._emit_delivery_unknown(updated)
            changed.append(updated)
        return changed


def claim_job(service: JobsService, *, timeout_s: float | None = None) -> JobClaimed | None:
    """函数式入口，委托给 JobsService.claim_job。"""
    return service.claim_job(timeout_s=timeout_s)


__all__ = ["Clock", "JobsService", "claim_job"]
