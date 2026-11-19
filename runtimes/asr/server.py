"""Sidecar HTTP：路径与 `_asr.AsrSidecarClient` 一致，全链路异步卸载阻塞操作，不在 import 时加载引擎。"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from _audio_codec import decode_audio_to_pcm16k
from _contracts import (
    ASR_STREAMING_BLOCKERS,
    ErrorCode,
    LogicError,
    RecognizeSessionCreate,
    RecognizeSessionCreated,
    SessionClosedEvent,
    SessionClosedReason,
    SessionErrorEvent,
    SessionFinalEvent,
    SessionPartialEvent,
    SttResponse,
)
from runtimes.asr.engine import AsrEngine
from runtimes.asr.protocol import (
    DEFAULT_IDLE_S,
    DEFAULT_SWEEP_INTERVAL_S,
    OFFICIAL_PARTIAL_POLICY,
    SIDECAR_CHUNK_HEADER,
    SIDECAR_SESSIONS_PATH,
    SIDECAR_TRANSCRIBE_PATH,
)
from runtimes.asr.scheduler import EngineScheduler
from runtimes.asr.sessions import (
    Clock,
    SidecarSessionManager,
    SubscriberOverflowError,
)


def _error(status: int, code: ErrorCode, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {
        "ok": False,
        "error": code.value if hasattr(code, "value") else str(code),
        "message": message,
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content=body)


def _logic_status(exc: LogicError) -> int:
    mapping = {
        ErrorCode.invalid_request: 400,
        ErrorCode.not_found: 404,
        ErrorCode.conflict: 409,
        ErrorCode.timeout: 504,
        ErrorCode.asr_unavailable: 503,
    }
    return mapping.get(exc.code, 500)


def _resolve_upload_content_type(
    content_type: str | None, filename: str | None
) -> str | None:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in ("", "application/octet-stream"):
        fname = (filename or "").strip().lower()
        if fname.endswith((".pcm", ".raw", ".s16le")):
            return "audio/pcm"
    return content_type


def wav_to_pcm_s16le(
    data: bytes,
    filename: str | None = None,
    *,
    content_type: str | None = None,
) -> bytes:
    """将上传音频解码为 16kHz mono s16le PCM，统一委托给 `_audio_codec.decode_audio_to_pcm16k`。"""
    effective_ct = _resolve_upload_content_type(content_type, filename)
    return decode_audio_to_pcm16k(data, content_type=effective_ct)


def _iter_engine_partials(event: object) -> list[SessionPartialEvent]:
    if event is None:
        return []
    if isinstance(event, SessionPartialEvent):
        return [event]
    if isinstance(event, Sequence) and not isinstance(event, (str, bytes, bytearray)):
        items: list[SessionPartialEvent] = []
        for item in event:
            if not isinstance(item, SessionPartialEvent):
                raise LogicError(ErrorCode.internal, "引擎 push_pcm 序列中含有非 SessionPartialEvent")
            items.append(item)
        return items
    raise LogicError(ErrorCode.internal, "引擎 push_pcm 必须返回 SessionPartialEvent、序列或 None")


def _format_sse(event: Any) -> str:
    payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
    name = payload.get("event") if isinstance(payload, dict) else None
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n" if name else f"data: {data}\n\n"


def create_sidecar_app(
    engine: AsrEngine,
    *,
    scheduler: EngineScheduler | None = None,
    clock: Clock = time.monotonic,
    idle_s: float = DEFAULT_IDLE_S,
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    sse_queue_max: int = 32,
    blockers: tuple[str, ...] = ASR_STREAMING_BLOCKERS,
) -> FastAPI:
    sched = scheduler or EngineScheduler()
    mgr = SidecarSessionManager(
        engine=engine,
        scheduler=sched,
        clock=clock,
        idle_s=idle_s,
        sse_queue_max=sse_queue_max,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = asyncio.Event()

        async def _sweep_loop() -> None:
            interval = max(0.05, float(sweep_interval_s))
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                except TimeoutError:
                    await asyncio.to_thread(mgr.sweep)

        task = asyncio.create_task(_sweep_loop())
        try:
            yield
        finally:
            stop_event.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="ollama-voice-input ASR sidecar",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(LogicError)
    async def _on_logic(_request: Request, exc: LogicError):
        return _error(_logic_status(exc), exc.code, exc.message, exc.details)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "loaded": bool(getattr(engine, "loaded", False)),
            "backend": getattr(engine, "resolved_backend", None)
            or getattr(engine, "backend", None),
            "device": getattr(engine, "resolved_device", None),
            "bind": "127.0.0.1",
            "unmet_blockers": list(blockers),
            "partial_policy": OFFICIAL_PARTIAL_POLICY,
        }

    def _transcribe_sync(
        raw_bytes: bytes,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        context: str | None,
    ) -> SttResponse:
        pcm = wav_to_pcm_s16le(raw_bytes, filename, content_type=content_type)
        if not getattr(engine, "loaded", True):
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load")
        sched.acquire_batch()
        try:
            return engine.transcribe(pcm, language=language, context=context)
        finally:
            sched.release_batch()

    @app.post(SIDECAR_TRANSCRIBE_PATH, response_model=SttResponse)
    async def transcribe(
        audio: UploadFile | None = File(default=None),
        language: str | None = Form(default=None),
        context: str | None = Form(default=None),
    ) -> SttResponse:
        if audio is None:
            raise LogicError(ErrorCode.invalid_request, "必须提供音频文件")
        data = await audio.read()
        return await asyncio.to_thread(
            _transcribe_sync,
            data,
            audio.filename,
            audio.content_type,
            language,
            context,
        )

    def _create_session_sync(payload: RecognizeSessionCreate) -> RecognizeSessionCreated:
        if not getattr(engine, "loaded", True):
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load")
        session_id = str(uuid.uuid4())
        sched.acquire_stream(session_id)
        try:
            engine.start_stream(session_id, language=payload.language, context=payload.context)
            mgr.register_open(session_id)
        except BaseException:
            if engine.has_stream(session_id):
                with suppress(Exception):
                    engine.cancel_stream(session_id)
            sched.release_stream(session_id)
            raise
        return RecognizeSessionCreated(session_id=session_id)

    @app.post(SIDECAR_SESSIONS_PATH, response_model=RecognizeSessionCreated)
    async def create_session(
        body: RecognizeSessionCreate | None = None,
    ) -> RecognizeSessionCreated:
        payload = body or RecognizeSessionCreate()
        return await asyncio.to_thread(_create_session_sync, payload)

    def _push_chunk_sync(session_id: str, index: int, pcm: bytes) -> dict[str, Any]:
        if len(pcm) % 2 != 0:
            raise LogicError(ErrorCode.invalid_request, "音频编码必须是 s16le PCM，长度为偶数")
        session = mgr.get_open(session_id)
        with session.infer_lock:
            if session.state != "open":
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            mgr.touch(session)
            event = engine.push_pcm(session_id, pcm)
            partials = _iter_engine_partials(event)
        for item in partials:
            mgr.publish(session, item)
        return {"accepted": True, "index": index}

    @app.post(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/chunks")
    async def upload_chunk(
        session_id: str,
        request: Request,
        x_chunk_index: str | None = Header(default=None, alias=SIDECAR_CHUNK_HEADER),
    ):
        if x_chunk_index is None or not str(x_chunk_index).strip():
            raise LogicError(ErrorCode.invalid_request, f"缺少 {SIDECAR_CHUNK_HEADER}")
        try:
            index = int(x_chunk_index)
        except ValueError as exc:
            raise LogicError(ErrorCode.invalid_request, "chunk index 必须是整数") from exc
        if index < 0:
            raise LogicError(ErrorCode.invalid_request, "chunk index 不能为负")
        pcm = await request.body()
        return await asyncio.to_thread(_push_chunk_sync, session_id, index, pcm)

    @app.get(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/events")
    async def events(session_id: str, request: Request) -> StreamingResponse:
        session = await asyncio.to_thread(mgr.get_open, session_id)
        sub = await asyncio.to_thread(mgr.subscribe, session)
        hook = getattr(engine, "on_events_subscribed", None)
        if callable(hook):
            early = await asyncio.to_thread(hook, session_id)
            for item in early or []:
                if item is not None:
                    await asyncio.to_thread(mgr.publish, session, item)

        async def generate():
            try:
                while True:
                    if await request.is_disconnected():
                        if session.state == "open":
                            await asyncio.to_thread(
                                mgr.close_session,
                                session,
                                SessionClosedReason.disconnected,
                                SessionErrorEvent(
                                    session_id=session_id,
                                    error=ErrorCode.invalid_request,
                                    message="SSE 在 finish 前断开，不调用 finish、不定稿",
                                ),
                            )
                        break
                    try:
                        event = await asyncio.to_thread(sub.poll, 0.2)
                    except SubscriberOverflowError:
                        overflow_err = SessionErrorEvent(
                            session_id=session_id,
                            error=ErrorCode.internal,
                            message="SSE 队列已满，连接已断开",
                        )
                        yield _format_sse(overflow_err)
                        if session.state == "open":
                            await asyncio.to_thread(
                                mgr.close_session,
                                session,
                                SessionClosedReason.error,
                            )
                        yield _format_sse(
                            SessionClosedEvent(
                                session_id=session_id,
                                reason=SessionClosedReason.error,
                            )
                        )
                        return
                    if event is None:
                        continue
                    yield _format_sse(event)
                    if getattr(event, "event", None) == "closed" or (
                        isinstance(event, dict) and event.get("event") == "closed"
                    ):
                        return
            finally:
                await asyncio.to_thread(mgr.unsubscribe, session, sub)

        return StreamingResponse(generate(), media_type="text/event-stream")

    def _finish_sync(session_id: str) -> dict[str, Any]:
        session = mgr.get_open(session_id)
        closed_cleanly = False
        try:
            with session.infer_lock:
                if session.state != "open":
                    raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
                final = engine.finish_stream(session_id)
            if not isinstance(final, SessionFinalEvent):
                raise LogicError(ErrorCode.asr_unavailable, "finish 必须返回 final 事件")
            mgr.publish(session, final)
            mgr.close_session(session, SessionClosedReason.finished)
            closed_cleanly = True
            return final.model_dump(mode="json")
        except LogicError as exc:
            mgr.close_session(
                session,
                SessionClosedReason.error,
                SessionErrorEvent(
                    session_id=session_id, error=exc.code, message=exc.message
                ),
            )
            closed_cleanly = True
            raise
        except Exception as exc:
            wrapped = LogicError(ErrorCode.asr_unavailable, f"定稿推理异常: {exc}")
            mgr.close_session(
                session,
                SessionClosedReason.error,
                SessionErrorEvent(
                    session_id=session_id,
                    error=wrapped.code,
                    message=wrapped.message,
                ),
            )
            closed_cleanly = True
            raise wrapped from exc
        finally:
            if not closed_cleanly:
                mgr.close_session(session, SessionClosedReason.error)
            else:
                sched.release_stream(session_id)

    @app.post(f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/finish")
    async def finish(session_id: str):
        return await asyncio.to_thread(_finish_sync, session_id)

    def _cancel_sync(session_id: str) -> SessionClosedEvent:
        session = mgr.get_open(session_id)
        try:
            with session.infer_lock:
                if engine.has_stream(session_id):
                    engine.cancel_stream(session_id)
            return SessionClosedEvent(
                session_id=session_id, reason=SessionClosedReason.cancelled
            )
        finally:
            mgr.close_session(session, SessionClosedReason.cancelled)

    @app.post(
        f"{SIDECAR_SESSIONS_PATH}/{{session_id}}/cancel",
        response_model=SessionClosedEvent,
    )
    async def cancel(session_id: str) -> SessionClosedEvent:
        return await asyncio.to_thread(_cancel_sync, session_id)

    app.state.engine = engine
    app.state.scheduler = sched
    app.state.session_manager = mgr
    app.state.sessions = mgr.sessions
    app.state.sweep = mgr.sweep
    app.state.blockers = blockers
    return app


__all__ = ["create_sidecar_app", "wav_to_pcm_s16le"]
