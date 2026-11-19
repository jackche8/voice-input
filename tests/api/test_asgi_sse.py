"""原始 ASGI 有限 SSE 发送 / 断连，以及有界队列与可注入时钟的清理。"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

import _v3_app
from _contracts import (
    ErrorCode,
    LogicError,
    ProductBusyEvent,
    RecognizeSessionCreate,
    SessionClosedReason,
)
from _v3.sessions import RecognizeSessionHub
from _v3_app import build_sandbox_runtime, create_app
from tests.api.asgi_client import call_asgi, collect_sse
from tests.api.conftest import TEST_TOKEN
from tests.api.providers import FixtureAsrProvider, fixture_transcribe


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


LOCAL_HEADERS = {"host": "127.0.0.1:17945", "origin": "https://127.0.0.1:17945"}
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def sse_app(tmp_path: Path):
    clock = FakeClock()
    runtime = build_sandbox_runtime(
        root=tmp_path / "sandbox",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        stream_provider=FixtureAsrProvider(final_text="fixture-final"),
        sse_queue_max=2,
        session_retain_closed_s=10.0,
        session_idle_open_s=20.0,
        clock=clock,
    )
    app = create_app(runtime)
    return app, runtime, clock


def test_product_sse_asgi_sends_idle_and_cleans_on_disconnect(sse_app):
    app, runtime, _clock = sse_app
    result = asyncio.run(
        collect_sse(
            app,
            path="/api/v1/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            disconnect_after_events=1,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    assert "text/event-stream" in result["headers"].get("content-type", "")
    assert result["events"][0]["event"] == "idle"
    assert runtime.events._listeners == []


def test_session_sse_asgi_emits_final_then_closed(sse_app):
    app, _runtime, _clock = sse_app
    created = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
    )
    sid = created["body"]["session_id"]
    hub: RecognizeSessionHub = app.state.sessions

    def _finish():
        hub.finish(sid)

    result = asyncio.run(
        collect_sse(
            app,
            path=f"/api/v1/recognize/sessions/{sid}/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            after_start=_finish,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    names = [item.get("event") for item in result["events"]]
    assert names[:2] == ["final", "closed"] or names == ["final", "closed"], result["events"]
    assert result["events"][0]["text"] == "fixture-final"
    assert result["events"][1]["reason"] == "finished"
    assert hub._sessions[sid].subscribers == []


def test_session_sse_asgi_disconnect_before_finish(sse_app):
    app, runtime, _clock = sse_app
    created = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
    )
    sid = created["body"]["session_id"]
    result = asyncio.run(
        collect_sse(
            app,
            path=f"/api/v1/recognize/sessions/{sid}/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            disconnect_after_events=0,
            timeout=1.0,
        )
    )
    # 主动 disconnect（尚未收到事件）后会话应记为 disconnected
    snap = runtime.finals.lookup(sid)
    assert snap is not None
    assert snap.closed_reason == SessionClosedReason.disconnected
    finish = asyncio.run(
        call_asgi(
            app,
            method="POST",
            path=f"/api/v1/recognize/sessions/{sid}/finish",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b"{}",
        )
    )
    assert finish["status"] == 409, finish
    assert app.state.sessions._sessions[sid].subscribers == []
    assert result["status"] in {200, None} or result["status"] == 200


def test_product_sse_overflow_visibly_terminates(sse_app):
    app, runtime, _clock = sse_app

    def flood():
        for _ in range(8):
            runtime.events.publish(ProductBusyEvent())

    result = asyncio.run(
        collect_sse(
            app,
            path="/api/v1/events",
            client=LOCAL,
            headers=LOCAL_HEADERS,
            after_idle=flood,
            timeout=2.0,
        )
    )
    assert result["status"] == 200, result
    names = [item.get("event") for item in result["events"]]
    assert "idle" in names
    assert "error" in names
    err = next(item for item in result["events"] if item.get("event") == "error")
    assert "队列已满" in err["message"]
    assert runtime.events._listeners == []


def test_session_eviction_and_idle_timeout_use_injected_clock(sse_app):
    app, runtime, clock = sse_app
    hub: RecognizeSessionHub = app.state.sessions
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    assert hub.has_session(sid)
    clock.advance(20.0)
    swept = hub.sweep()
    assert sid in swept["idle_disconnected"]
    assert runtime.finals.lookup(sid) is not None
    assert runtime.finals.lookup(sid).closed_reason == SessionClosedReason.disconnected
    clock.advance(10.0)
    evicted = hub.sweep()
    assert sid in evicted["evicted"]
    assert not hub.has_session(sid)
    # 状态仍可从 finals 查询，不因驱逐丢失
    assert runtime.finals.lookup(sid).closed_reason == SessionClosedReason.disconnected


def test_finished_session_retained_until_clock_advances(sse_app):
    app, _runtime, clock = sse_app
    hub: RecognizeSessionHub = app.state.sessions
    created = hub.create(RecognizeSessionCreate(language="zh"))
    sid = created.session_id
    hub.finish(sid)
    assert hub.has_session(sid)
    clock.advance(9.0)
    assert sid not in hub.sweep()["evicted"]
    assert hub.has_session(sid)
    clock.advance(1.0)
    assert sid in hub.sweep()["evicted"]
    assert not hub.has_session(sid)


class SlowCancelAsrProvider(FixtureAsrProvider):
    def __init__(self, delay_s: float = 0.2) -> None:
        super().__init__(final_text="fixture-final")
        self.delay_s = delay_s
        self.cancel_calls: list[str] = []

    def cancel(self, session_id: str) -> None:
        time.sleep(self.delay_s)
        self.cancel_calls.append(session_id)
        super().cancel(session_id)


def test_session_sse_disconnect_does_not_block_event_loop_with_slow_cancel(tmp_path: Path):
    provider = SlowCancelAsrProvider(delay_s=0.2)
    runtime = build_sandbox_runtime(
        root=tmp_path / "slow-cancel",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        stream_provider=provider,
    )
    app = create_app(runtime)

    async def scenario() -> tuple[str, int, float]:
        created = await call_asgi(
            app,
            method="POST",
            path="/api/v1/recognize/sessions",
            client=LOCAL,
            headers={**LOCAL_HEADERS, "content-type": "application/json"},
            body=b'{"language":"zh"}',
        )
        sid = created["body"]["session_id"]

        disconnect = asyncio.Event()
        sent_request = False
        started = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": f"/api/v1/recognize/sessions/{sid}/events",
            "raw_path": f"/api/v1/recognize/sessions/{sid}/events".encode(),
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in LOCAL_HEADERS.items()],
            "client": LOCAL,
            "server": ("127.0.0.1", 17945),
        }

        max_gap = 0.0
        ticks = 0
        stop_hb = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal max_gap, ticks
            last = time.perf_counter()
            while not stop_hb.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                max_gap = max(max_gap, now - last)
                last = now
                ticks += 1

        hb_task = asyncio.create_task(heartbeat())
        sse_task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
            # 让 generate() 进入 await subscriber.get_async()，确保走 CancelledError 清理路径
            await asyncio.sleep(0.03)
            disconnect.set()
            await asyncio.wait_for(sse_task, timeout=3.0)
        finally:
            stop_hb.set()
            await hb_task
        return sid, ticks, max_gap

    sid, ticks, max_gap = asyncio.run(scenario())
    assert ticks >= 10, f"心跳次数过少，疑似事件循环被阻塞: ticks={ticks}, max_gap={max_gap:.4f}s"
    assert max_gap < 0.15, f"SSE 断连清理阻塞了事件循环: max_gap={max_gap:.4f}s (cancel delay=0.2s)"
    assert sid in provider.cancel_calls
    snap = runtime.finals.lookup(sid)
    assert snap is not None
    assert snap.closed_reason == SessionClosedReason.disconnected
    assert app.state.sessions._sessions[sid].state == "disconnected"
    assert app.state.sessions._sessions[sid].subscribers == []


def test_sweep_skips_concurrently_removed_session_and_propagates_real_error(sse_app, monkeypatch):
    app, runtime, clock = sse_app
    hub: RecognizeSessionHub = app.state.sessions
    sid1 = hub.create(RecognizeSessionCreate(language="zh")).session_id
    sid2 = hub.create(RecognizeSessionCreate(language="zh")).session_id
    clock.advance(20.0)

    real_mark = hub.mark_disconnected

    def flaky_mark(sid: str) -> None:
        if sid == sid1:
            raise LogicError(ErrorCode.not_found, f"会话已并发移除: {sid}")
        real_mark(sid)

    monkeypatch.setattr(hub, "mark_disconnected", flaky_mark)
    swept = hub.sweep()
    assert sid1 not in swept["idle_disconnected"]
    assert swept["idle_disconnected"] == [sid2]
    assert runtime.finals.lookup(sid2) is not None
    assert runtime.finals.lookup(sid2).closed_reason == SessionClosedReason.disconnected

    sid3 = hub.create(RecognizeSessionCreate(language="zh")).session_id
    clock.advance(20.0)

    def broken_mark(sid: str) -> None:
        raise RuntimeError(f"provider boom for {sid}")

    monkeypatch.setattr(hub, "mark_disconnected", broken_mark)
    with pytest.raises(RuntimeError, match="provider boom"):
        hub.sweep()


def test_lifespan_sweep_loop_survives_unexpected_exception(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(_v3_app, "SESSION_SWEEP_INTERVAL_S", 0.02)
    runtime = build_sandbox_runtime(
        root=tmp_path / "sweep-loop",
        token=TEST_TOKEN,
        transcribe=fixture_transcribe,
        stream_provider=FixtureAsrProvider(final_text="fixture-final"),
    )
    app = create_app(runtime)
    hub: RecognizeSessionHub = app.state.sessions
    calls = 0
    second_round = threading.Event()
    real_sweep = hub.sweep

    def flaky_sweep(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient sweep failure")
        second_round.set()
        return real_sweep(*args, **kwargs)

    monkeypatch.setattr(hub, "sweep", flaky_sweep)

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            ok = await asyncio.to_thread(second_round.wait, 2.0)
            assert ok, f"后台 sweep 循环在首次异常后未能继续下一轮: calls={calls}"

    with caplog.at_level(logging.ERROR, logger="_v3_app"):
        asyncio.run(run_lifespan())

    assert calls >= 2
    assert any("transient sweep failure" in rec.getMessage() or (rec.exc_info and "transient sweep failure" in str(rec.exc_info[1])) for rec in caplog.records)
