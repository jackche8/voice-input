from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _contracts import (
    AckResult,
    ErrorCode,
    JobAckRequest,
    JobQueryFound,
    JobRecord,
    JobState,
    LogicError,
    ProductIdleEvent,
    SessionClosedReason,
)
from _events import EventBus
from _jobs import MAX_TERMINAL_JOBS, JobStore
from _jobs_service import JobsService, claim_job
from _session_finals import MAX_SESSION_FINALS, SessionFinalIndex


def _rec(job_id: str, focus_id: str, text: str = "t", state: JobState = JobState.queued) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        state=state,
        text=text,
        focus_id=focus_id,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def test_put_get_and_per_job_focus(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(_rec("j1", "focus-a"))
    store.put(_rec("j2", "focus-b"))
    assert store.get("j1").focus_id == "focus-a"
    assert store.get("j2").focus_id == "focus-b"
    assert store.get("missing") is None


def test_update_same_claim_late_ack_is_storage_only(tmp_path: Path):
    """L1 只保存字段，不实现重投。晚到 ack 由调用方再次 update 收敛。"""
    store = JobStore(tmp_path / "jobs.json")
    store.put(
        _rec("j1", "f", state=JobState.delivery_unknown).model_copy(
            update={"claim_id": "c1", "detail": "ack lost"}
        )
    )
    updated = store.update("j1", state=JobState.done, detail="late ack complete")
    assert updated.state == JobState.done
    assert updated.claim_id == "c1"
    assert store.get("j1").state == JobState.done


def test_update_missing_and_invalid_state(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(LogicError) as ei:
        store.update("nope", state=JobState.done)
    assert ei.value.code.value == "not_found"
    store.put(_rec("j1", "f"))
    with pytest.raises(LogicError) as ei2:
        store.update("j1", state="running")
    assert ei2.value.code.value == "invalid_request"


def test_corrupt_file_is_visible(tmp_path: Path):
    path = tmp_path / "jobs.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(LogicError) as ei:
        JobStore(path).load()
    assert ei.value.code.value == "jobs_io"
    assert "损坏" in ei.value.message or "JSON" in ei.value.message or "schema" in ei.value.message


def test_concurrent_puts_are_not_lost(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")

    def write(i: int) -> None:
        store.put(_rec(f"job-{i}", f"focus-{i}", text=str(i)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(20)))
    jobs = {j.job_id: j for j in store.list_jobs()}
    assert len(jobs) == 20
    for i in range(20):
        assert jobs[f"job-{i}"].focus_id == f"focus-{i}"
        assert jobs[f"job-{i}"].text == str(i)
    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1


def test_compare_and_update_only_one_winner(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(_rec("j1", "focus-a", state=JobState.queued))
    barrier = threading.Barrier(2)
    winners: list[str] = []
    conflicts: list[str] = []
    lock = threading.Lock()

    def attempt(claim_id: str) -> None:
        barrier.wait()
        try:
            updated = store.update_if(
                "j1",
                expected={"state": JobState.queued},
                fields={"state": JobState.claimed, "claim_id": claim_id},
            )
            with lock:
                winners.append(updated.claim_id or "")
        except LogicError as exc:
            assert exc.code == ErrorCode.conflict
            with lock:
                conflicts.append(claim_id)

    t1 = threading.Thread(target=attempt, args=("c-left",))
    t2 = threading.Thread(target=attempt, args=("c-right",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(winners) == 1
    assert len(conflicts) == 1
    current = store.get("j1")
    assert current is not None
    assert current.state == JobState.claimed
    assert current.claim_id == winners[0]


def test_update_if_missing_and_mismatched_condition(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(LogicError) as ei:
        store.update_if("nope", expected={"state": JobState.queued}, fields={"state": JobState.claimed})
    assert ei.value.code == ErrorCode.not_found
    store.put(_rec("j1", "f", state=JobState.claimed))
    with pytest.raises(LogicError) as ei2:
        store.update_if("j1", expected={"state": JobState.queued}, fields={"state": JobState.done})
    assert ei2.value.code == ErrorCode.conflict
    assert store.get("j1").state == JobState.claimed


def test_put_if_absent_writes_once_and_conflicts_without_overwrite(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    first = store.put_if_absent(_rec("j1", "focus-a", text="keep"))
    assert first.text == "keep"
    with pytest.raises(LogicError) as ei:
        store.put_if_absent(_rec("j1", "focus-b", text="overwrite"))
    assert ei.value.code.value == "conflict"
    assert store.get("j1").text == "keep"
    assert store.get("j1").focus_id == "focus-a"
    missing = JobStore(tmp_path / "missing-dir" / "jobs.json")
    created = missing.put_if_absent(_rec("j2", "f2", text="new"))
    assert created.job_id == "j2"
    assert missing.get("j2").text == "new"


def test_condition_wakeup_zero_polling_across_same_path_instances(tmp_path: Path):
    path = tmp_path / "jobs.json"
    consumer_store = JobStore(path)
    producer_store = JobStore(path)
    bus = EventBus()
    svc = JobsService(consumer_store, bus)

    started = threading.Event()
    result_box: dict[str, object] = {}

    def consumer() -> None:
        started.set()
        t0 = time.monotonic()
        claimed = claim_job(svc, timeout_s=3.0)
        result_box["claimed"] = claimed
        result_box["dt"] = time.monotonic() - t0

    worker = threading.Thread(target=consumer, daemon=True)
    worker.start()
    assert started.wait(timeout=1.0)
    time.sleep(0.04)
    producer_store.enqueue(_rec("j-wake", "focus-w", text="即时唤醒"))
    worker.join(timeout=1.5)
    assert not worker.is_alive()
    claimed = result_box["claimed"]
    assert claimed is not None and claimed.job_id == "j-wake"
    assert result_box["dt"] < 0.5  # Condition 信号直接唤醒，远小于 3.0s 超时


def test_prune_terminal_jobs_keeps_recent_500_and_all_active(tmp_path: Path):
    assert MAX_TERMINAL_JOBS == 500
    store = JobStore(tmp_path / "jobs.json")
    doc = store.load()
    for i in range(515):
        ts = f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"
        doc.jobs[f"done-{i:04d}"] = JobRecord(
            job_id=f"done-{i:04d}",
            state=JobState.done if i % 2 == 0 else JobState.failed,
            text=str(i),
            focus_id="f",
            created_at=ts,
            updated_at=ts,
        )
    # 早期活动任务绝不能被终态裁剪误删
    doc.jobs["active-queued"] = _rec("active-queued", "f", state=JobState.queued)
    doc.jobs["active-claimed"] = _rec("active-claimed", "f", state=JobState.claimed)
    store._with_lock(lambda: store._write_unlocked(doc))

    reloaded = store.load()
    assert "active-queued" in reloaded.jobs
    assert "active-claimed" in reloaded.jobs
    terminals = [j for j in reloaded.jobs.values() if j.state in (JobState.done, JobState.failed)]
    assert len(terminals) == MAX_TERMINAL_JOBS
    assert "done-0000" not in reloaded.jobs
    assert "done-0014" not in reloaded.jobs
    assert "done-0015" in reloaded.jobs
    assert "done-0514" in reloaded.jobs


def test_get_expires_stale_lease_and_repeat_ack_is_idempotent(tmp_path: Path):
    bus = EventBus()
    events: list[object] = []
    bus.subscribe(events.append)
    clock = {"now": datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)}
    store = JobStore(tmp_path / "jobs.json")
    svc = JobsService(store, bus, clock=lambda: clock["now"], claim_lease_s=30.0)

    svc.enqueue(
        JobRecord(
            job_id="j-lease",
            state=JobState.queued,
            text="lease-check",
            focus_id="f1",
            created_at="2026-06-01T00:00:00+00:00",
            updated_at="2026-06-01T00:00:00+00:00",
        )
    )
    claimed = svc.claim_job(timeout_s=0.0)
    assert claimed is not None

    # 租约过期后直接调用 get() 必须先收敛为 delivery_unknown
    clock["now"] = clock["now"] + timedelta(seconds=31)
    queried = svc.get("j-lease")
    assert isinstance(queried, JobQueryFound)
    assert queried.job.state == JobState.delivery_unknown
    assert sum(1 for e in events if getattr(e, "event", None) == "delivery_unknown") == 1

    # 晚到 complete ack 收敛为 done；同 (claim_id, 目标 state) 重复 ack 幂等返回且不重发事件
    req_ok = JobAckRequest(claim_id=claimed.claim_id, result=AckResult.complete)
    first_ack = svc.ack("j-lease", req_ok)
    second_ack = svc.ack("j-lease", req_ok)
    assert first_ack.state == JobState.done
    assert second_ack.state == JobState.done
    assert sum(1 for e in events if getattr(e, "event", None) == "typed") == 1

    # 同 claim_id 但试图把已 done 改成 failed 仍须显式报 conflict
    with pytest.raises(LogicError) as conflict_err:
        svc.ack("j-lease", JobAckRequest(claim_id=claimed.claim_id, result=AckResult.none))
    assert conflict_err.value.code == ErrorCode.conflict


def test_event_bus_rlock_and_session_finals_512_fifo_eviction():
    assert MAX_SESSION_FINALS == 512
    idx = SessionFinalIndex()
    for i in range(600):
        idx.record_final(f"sess-{i}", f"text-{i}", "Chinese")
    # 更新已存在条目不应改变总容量上限
    idx.record_closed("sess-599", SessionClosedReason.finished)
    idx.bind_job("sess-599", "job-599")
    assert idx.lookup("sess-0") is None
    assert idx.lookup("sess-87") is None
    assert idx.lookup("sess-88") is not None
    snap_last = idx.lookup("sess-599")
    assert snap_last is not None and snap_last.bound_job_id == "job-599"

    bus = EventBus()
    received: list[str] = []
    lock = threading.Lock()

    def listener(ev) -> None:
        with lock:
            received.append(ev.event)

    bus.subscribe(listener)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: bus.publish(ProductIdleEvent()), range(30)))
    bus.unsubscribe(listener)
    bus.publish(ProductIdleEvent())
    assert len(received) == 30


def test_wait_and_claim_next_wakes_on_lease_expiry_and_runs_callback_outside_lock(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    now_iso = datetime.now(timezone.utc).isoformat()
    store.enqueue(
        JobRecord(
            job_id="j-exp",
            state=JobState.queued,
            text="lease-wake",
            focus_id="f-wake",
            created_at=now_iso,
            updated_at=now_iso,
        )
    )
    first = store.wait_and_claim_next(timeout_s=0.0, stale_after_s=0.1)
    assert first is not None and first.job_id == "j-exp"

    seen: list[tuple[float, JobRecord]] = []
    lock_free: dict[str, bool] = {"ok": False}
    t0 = time.monotonic()

    def on_expired(item: JobRecord) -> None:
        dt = time.monotonic() - t0
        # 另一线程尝试非阻塞获取 store.condition：仅当回调在 self._cond 锁外派发时才会成功
        def probe() -> None:
            acquired = store.condition.acquire(blocking=False)
            if acquired:
                try:
                    lock_free["ok"] = True
                finally:
                    store.condition.release()

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(timeout=0.5)
        seen.append((dt, item))

    result = store.wait_and_claim_next(
        timeout_s=1.0,
        stale_after_s=0.1,
        on_expired=on_expired,
    )
    assert result is None
    assert len(seen) == 1
    dt_fired, expired_rec = seen[0]
    assert expired_rec.job_id == "j-exp"
    assert expired_rec.state == JobState.delivery_unknown
    assert dt_fired < 0.5  # 租约 0.1s 到期即唤醒收敛，绝不白睡满 1.0s
    assert lock_free["ok"] is True
    assert store.get("j-exp").state == JobState.delivery_unknown


def test_job_store_public_expire_stale_claims(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    t0 = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    store.put(
        JobRecord(
            job_id="j-pub",
            state=JobState.claimed,
            claim_id="c-pub",
            text="pub",
            focus_id="f-pub",
            created_at=t0.isoformat(),
            updated_at=t0.isoformat(),
        )
    )
    assert store.expire_stale_claims(stale_after_s=10.0, moment=t0 + timedelta(seconds=5)) == []
    expired = store.expire_stale_claims(stale_after_s=10.0, moment=t0 + timedelta(seconds=11))
    assert len(expired) == 1
    assert expired[0].job_id == "j-pub"
    assert expired[0].state == JobState.delivery_unknown
    assert store.get("j-pub").state == JobState.delivery_unknown
