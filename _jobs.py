"""jobs.json 存取层：原子写、并发安全、Condition 零轮询唤醒与终态历史裁剪。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _contracts import ErrorCode, JobRecord, JobState, JobStoreDocument, LogicError

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows/非 POSIX 环境无 fcntl
    fcntl = None  # type: ignore[assignment]

MAX_TERMINAL_JOBS = 500
TERMINAL_JOB_STATES = frozenset(
    {JobState.done, JobState.failed, JobState.delivery_unknown}
)

_REGISTRY_GUARD = threading.Lock()
_PATH_PRIMITIVES: dict[Path, tuple[threading.RLock, threading.Condition]] = {}


def _primitives_for(path: Path) -> tuple[threading.RLock, threading.Condition]:
    resolved = path.resolve()
    with _REGISTRY_GUARD:
        pair = _PATH_PRIMITIVES.get(resolved)
        if pair is None:
            rlock = threading.RLock()
            cond = threading.Condition(rlock)
            pair = (rlock, cond)
            _PATH_PRIMITIVES[resolved] = pair
        return pair


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _prune_terminal_jobs(doc: JobStoreDocument, max_terminal: int = MAX_TERMINAL_JOBS) -> None:
    """保留全部活动态任务（queued/claimed），并将终态记录裁剪至最近 max_terminal 条。"""
    if max_terminal < 0:
        return
    indexed_terminals: list[tuple[str, int, str]] = []
    for idx, (job_id, rec) in enumerate(doc.jobs.items()):
        if rec.state in TERMINAL_JOB_STATES:
            sort_ts = rec.updated_at or rec.created_at or ""
            indexed_terminals.append((sort_ts, idx, job_id))
    if len(indexed_terminals) <= max_terminal:
        return
    indexed_terminals.sort(key=lambda item: (item[0], item[1]))
    keep_terminal_ids = {
        job_id for _ts, _idx, job_id in indexed_terminals[-max_terminal:]
    } if max_terminal > 0 else set()
    doc.jobs = {
        job_id: rec
        for job_id, rec in doc.jobs.items()
        if rec.state not in TERMINAL_JOB_STATES or job_id in keep_terminal_ids
    }


class JobStore:
    """持久化存取与进程内同路径 Condition 事件唤醒。"""

    def __init__(self, path: Path, *, max_terminal_jobs: int = MAX_TERMINAL_JOBS) -> None:
        self.path = Path(path)
        self.max_terminal_jobs = max_terminal_jobs
        self._thread_lock, self._cond = _primitives_for(self.path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @property
    def condition(self) -> threading.Condition:
        return self._cond

    def _empty(self) -> JobStoreDocument:
        return JobStoreDocument()

    def _read_unlocked(self) -> JobStoreDocument:
        if not self.path.exists():
            return self._empty()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 无法读取: {exc}") from exc
        if not raw.strip():
            return self._empty()
        try:
            data = json.loads(raw)
            return JobStoreDocument.model_validate(data)
        except Exception as exc:
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 损坏或不符合 schema: {exc}") from exc

    def _write_unlocked(self, doc: JobStoreDocument) -> None:
        _prune_terminal_jobs(doc, self.max_terminal_jobs)
        text = json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".jobs.", suffix=".tmp", dir=str(self.path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise LogicError(ErrorCode.jobs_io, f"jobs.json 无法写入: {exc}") from exc

    def _lock_file(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _with_lock(self, fn):
        with self._thread_lock:
            fh = self._lock_file()
            try:
                return fn()
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                fh.close()

    def _notify_waiters(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def load(self) -> JobStoreDocument:
        return self._with_lock(self._read_unlocked)

    def get(self, job_id: str) -> JobRecord | None:
        doc = self.load()
        return doc.jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        doc = self.load()
        return list(doc.jobs.values())

    def put(self, record: JobRecord) -> JobRecord:
        validated = JobRecord.model_validate(record.model_dump())

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            doc.jobs[validated.job_id] = validated
            self._write_unlocked(doc)
            return doc.jobs.get(validated.job_id, validated)

        with self._cond:
            saved = self._with_lock(_op)
            if validated.state in (JobState.queued, JobState.claimed):
                self._cond.notify_all()
            return saved

    def enqueue(self, record: JobRecord) -> JobRecord:
        """原子入队并立即 notify_all 唤醒等待中的 claim 线程。"""
        return self.put_if_absent(record)

    def update(self, job_id: str, **fields: object) -> JobRecord:
        def _op() -> JobRecord:
            doc = self._read_unlocked()
            current = doc.jobs.get(job_id)
            if current is None:
                raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
            data = current.model_dump()
            data.update(fields)
            data["job_id"] = job_id
            try:
                updated = JobRecord.model_validate(data)
            except Exception as exc:
                raise LogicError(ErrorCode.invalid_request, f"job 更新不符合 schema: {exc}") from exc
            doc.jobs[job_id] = updated
            self._write_unlocked(doc)
            return updated

        with self._cond:
            updated = self._with_lock(_op)
            if updated.state in (JobState.queued, JobState.claimed):
                self._cond.notify_all()
            return updated

    def update_if(
        self,
        job_id: str,
        *,
        expected: dict[str, object],
        fields: dict[str, object],
    ) -> JobRecord:
        """锁内按字段等值条件更新。条件不满足则 conflict，找不到则 not_found。"""

        def _norm(value: object) -> object:
            return value.value if hasattr(value, "value") else value

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            current = doc.jobs.get(job_id)
            if current is None:
                raise LogicError(ErrorCode.not_found, f"job 不存在: {job_id}")
            data = current.model_dump()
            for key, want in expected.items():
                if _norm(data.get(key)) != _norm(want):
                    raise LogicError(
                        ErrorCode.conflict,
                        f"job 条件更新不匹配: {key}",
                        details={
                            "job_id": job_id,
                            "field": key,
                            "expected": _norm(want),
                            "actual": _norm(data.get(key)),
                        },
                    )
            data.update(fields)
            data["job_id"] = job_id
            try:
                updated = JobRecord.model_validate(data)
            except Exception as exc:
                raise LogicError(ErrorCode.invalid_request, f"job 更新不符合 schema: {exc}") from exc
            doc.jobs[job_id] = updated
            self._write_unlocked(doc)
            return updated

        with self._cond:
            updated = self._with_lock(_op)
            if updated.state in (JobState.queued, JobState.claimed):
                self._cond.notify_all()
            return updated

    def put_if_absent(self, record: JobRecord) -> JobRecord:
        """锁内仅在 job_id 不存在时写入；若是 queued 记录则唤醒全部等待者。"""
        validated = JobRecord.model_validate(record.model_dump())

        def _op() -> JobRecord:
            doc = self._read_unlocked()
            if validated.job_id in doc.jobs:
                raise LogicError(
                    ErrorCode.conflict,
                    f"job 已存在: {validated.job_id}",
                    details={"job_id": validated.job_id},
                )
            doc.jobs[validated.job_id] = validated
            self._write_unlocked(doc)
            return doc.jobs.get(validated.job_id, validated)

        with self._cond:
            created = self._with_lock(_op)
            self._cond.notify_all()
            return created

    def _expire_stale_and_next_expiry_unlocked(
        self,
        doc: JobStoreDocument,
        *,
        stale_after_s: float,
        moment: datetime,
    ) -> tuple[list[JobRecord], float | None]:
        """在已持文件锁的文档对象上收敛过期租约，并计算未过期 claimed 任务的最早到期剩余秒数。"""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        expired: list[JobRecord] = []
        next_expiry_s: float | None = None
        iso_now = _iso(moment)
        lease = timedelta(seconds=stale_after_s)
        for job_id, job in list(doc.jobs.items()):
            if job.state != JobState.claimed:
                continue
            stamped = _parse_iso(job.updated_at) or _parse_iso(job.created_at)
            if stamped is None:
                unparsable = True
            else:
                unparsable = False
                if stamped.tzinfo is None:
                    stamped = stamped.replace(tzinfo=timezone.utc)
                elapsed = moment - stamped
                if elapsed < lease:
                    until_expiry = (lease - elapsed).total_seconds()
                    if next_expiry_s is None or until_expiry < next_expiry_s:
                        next_expiry_s = until_expiry
                    continue
            detail = "claim timestamp unparsable" if unparsable else "claim timeout"
            updated = job.model_copy(
                update={
                    "state": JobState.delivery_unknown,
                    "updated_at": iso_now,
                    "detail": detail,
                }
            )
            doc.jobs[job_id] = updated
            expired.append(updated)
        return expired, next_expiry_s

    def expire_stale_unlocked(
        self,
        doc: JobStoreDocument,
        *,
        stale_after_s: float,
        moment: datetime,
    ) -> list[JobRecord]:
        """在已持文件锁的文档对象上收敛过期租约：claimed → delivery_unknown（绝不退回 queued）。"""
        expired, _next_expiry_s = self._expire_stale_and_next_expiry_unlocked(
            doc, stale_after_s=stale_after_s, moment=moment
        )
        return expired

    def expire_stale_claims(
        self,
        *,
        stale_after_s: float,
        moment: datetime | None = None,
    ) -> list[JobRecord]:
        """原子收敛所有已过期租约：claimed → delivery_unknown（不退回 queued）。"""
        effective_moment = moment or _utc_now()

        def _op() -> list[JobRecord]:
            doc = self._read_unlocked()
            expired = self.expire_stale_unlocked(
                doc, stale_after_s=stale_after_s, moment=effective_moment
            )
            if expired:
                self._write_unlocked(doc)
            return expired

        return self._with_lock(_op)

    def wait_and_claim_next(
        self,
        *,
        timeout_s: float = 0.0,
        stale_after_s: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        on_expired: Callable[[JobRecord], None] | None = None,
    ) -> JobRecord | None:
        """零轮询事件唤醒领取下一条 queued 任务；超时无任务返回 None。

        重要约束：
        1. Condition.wait() 期间绝不持有文件锁 flock，防止阻塞其他写者入队；
        2. 存在未过期 claimed 任务时，等待上限收紧至其最早租约到期时刻，及时收敛并派发回调；
        3. on_expired 回调必须在 self._cond 锁外执行，避免锁顺序反转死锁与阻塞入队。
        """
        if timeout_s < 0:
            raise LogicError(ErrorCode.invalid_request, "timeout_s 不能为负")
        now_fn = clock or _utc_now
        deadline = time.monotonic() + float(timeout_s)

        def _try_claim_once() -> tuple[JobRecord | None, list[JobRecord], float | None]:
            moment = now_fn()
            doc = self._read_unlocked()
            expired, next_expiry_s = self._expire_stale_and_next_expiry_unlocked(
                doc, stale_after_s=stale_after_s, moment=moment
            )
            queued = [
                job for job in doc.jobs.values() if job.state == JobState.queued
            ]
            claimed: JobRecord | None = None
            if queued:
                queued.sort(key=lambda item: (item.created_at, item.job_id))
                target = queued[0]
                claimed = target.model_copy(
                    update={
                        "state": JobState.claimed,
                        "claim_id": str(uuid.uuid4()),
                        "updated_at": _iso(moment),
                    }
                )
                doc.jobs[claimed.job_id] = claimed
            if expired or claimed is not None:
                self._write_unlocked(doc)
            return claimed, expired, next_expiry_s

        while True:
            with self._cond:
                claimed_rec, expired_list, next_expiry_s = self._with_lock(_try_claim_once)
                need_callback = bool(expired_list and on_expired is not None)
                if claimed_rec is None and not need_callback:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return None
                    wait_timeout = remaining
                    if next_expiry_s is not None:
                        wait_timeout = min(wait_timeout, max(next_expiry_s, 0.001))
                    self._cond.wait(timeout=wait_timeout)
                    continue
            if expired_list and on_expired is not None:
                for item in expired_list:
                    on_expired(item)
            if claimed_rec is not None:
                return claimed_rec
            if deadline - time.monotonic() <= 0.0:
                return None


__all__ = [
    "MAX_TERMINAL_JOBS",
    "TERMINAL_JOB_STATES",
    "JobStore",
]
