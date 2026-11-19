"""按住说话：绑定本句 job_id 与按下时焦点 → 开麦推流识别会话 → 松开 finish → 定稿入业务。

- 过短（<0.15s）：取消会话，不调业务。
- 看门狗 120s：超时强制调用 stop()（= finish，不是 cancel）。
- 热词上下文：`context_provider` 在后台上传线程读取并传入 `create_session(..., context=...)`，绝不阻塞 UI 主线程。
- 连续快速录音防 409：每句按下立即开麦并把 PCM 块缓存在本地队列；后台上传线程经前序会话事件与
  `_session_gate` 串行门控，严格等待前一句 `finish/cancel` 完成后再发下一句 `create_session`。
不补句号、不润色、不注入：定稿后的处理全在服务端业务与 jobs 投递。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from _capture import AudioSource, SessionCapture
from _contracts import ErrorCode
from _desktop.api import api_error_code, api_error_reason
from _desktop.status import PlatformStatus
from _focus import FocusProbe

DEFAULT_PREV_WAIT_TIMEOUT_S = 15.0


def new_job_id() -> str:
    return f"desk-{uuid.uuid4().hex}"


def format_keywords_context(keywords: Any) -> str | None:
    """将配置中的 `custom_keywords` 列表规范拼接为逗号分隔的热词提示串。"""
    if not keywords:
        return None
    if isinstance(keywords, str):
        cleaned = keywords.strip()
        return cleaned or None
    items: list[str] = []
    seen: set[str] = set()
    for raw in keywords:
        word = str(raw or "").strip()
        if word and word not in seen:
            seen.add(word)
            items.append(word)
    return ", ".join(items) if items else None


def build_keywords_context_provider(
    api: Any,
    *,
    ttl_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], str | None]:
    """构造带轻量 TTL 缓存的热词上下文读取器（供上传后台线程安全调用，绝不阻塞 UI 线程）。"""
    lock = threading.Lock()
    state: dict[str, Any] = {"fetched_at": -1e9, "value": None}

    def _provider() -> str | None:
        now = clock()
        with lock:
            if (now - float(state["fetched_at"])) < ttl_s:
                return state["value"]
        try:
            cfg = api.get_config() or {}
            value = format_keywords_context(cfg.get("custom_keywords"))
        except Exception as exc:
            print(f"[voice] 读取热词配置失败，沿用缓存: {exc}", flush=True)
            with lock:
                return state["value"]
        with lock:
            state["fetched_at"] = clock()
            state["value"] = value
        return value

    return _provider


class SessionUpload:
    """一句话的会话上传：等待前一句 sidecar 释放 → create → chunks → finish/cancel → utterances。"""

    def __init__(
        self,
        api: Any,
        status: PlatformStatus,
        *,
        job_id: str,
        language: str | None,
        context: str | None = None,
        context_provider: Callable[[], str | None] | None = None,
        prev_upload: "SessionUpload | None" = None,
        session_gate: threading.Lock | None = None,
        prev_wait_timeout_s: float = DEFAULT_PREV_WAIT_TIMEOUT_S,
    ) -> None:
        self.api = api
        self.status = status
        self.job_id = job_id
        self.language = language
        self.context = (context or "").strip() or None
        self._context_provider = context_provider
        self._prev_upload = prev_upload
        self._session_gate = session_gate
        self._prev_wait_timeout_s = max(0.0, float(prev_wait_timeout_s))

        self.session_id: str | None = None
        self.outcome: str | None = None
        self._items: queue.Queue = queue.Queue()
        self._session_closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"session-{job_id}", daemon=True)

    def begin(self) -> None:
        self._thread.start()

    def chunk(self, index: int, pcm: bytes) -> None:
        self._items.put(("chunk", index, pcm))

    def complete(self, focus_id: str) -> None:
        self._items.put(("finish", focus_id))

    def cancel(self) -> None:
        self._items.put(("cancel",))

    def wait_finished(self, timeout: float | None = None) -> bool:
        """等待本句的 sidecar 识别会话完成（finish / cancel / 异常放弃），释放单流槽位。"""
        return self._session_closed.wait(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _resolve_context(self) -> str | None:
        if self.context is not None:
            return self.context
        if self._context_provider is None:
            return None
        try:
            raw = self._context_provider()
        except Exception as exc:
            print(f"[voice] 获取热词上下文失败，将无热词继续识别: {exc}", flush=True)
            return None
        cleaned = (str(raw) if raw is not None else "").strip()
        self.context = cleaned or None
        return self.context

    def _run(self) -> None:
        prev = self._prev_upload
        self._prev_upload = None
        if prev is not None:
            prev.wait_finished(timeout=self._prev_wait_timeout_s)

        ctx = self._resolve_context()
        finish_focus: str | None = None
        need_drain = False
        gate = self._session_gate

        if gate is not None:
            gate.acquire()
        try:
            try:
                if ctx:
                    self.session_id = self.api.create_session(language=self.language, context=ctx)
                else:
                    self.session_id = self.api.create_session(language=self.language)
            except Exception as exc:
                self._fail(
                    f"识别会话无法开始：{api_error_reason(exc)}",
                    code=api_error_code(exc, ErrorCode.asr_unavailable),
                )
                need_drain = True
            else:
                while True:
                    item = self._items.get()
                    kind = item[0]
                    try:
                        if kind == "chunk":
                            self.api.upload_chunk(self.session_id, item[1], item[2])
                            continue
                        if kind == "cancel":
                            self.api.cancel_session(self.session_id)
                            self.outcome = "cancelled"
                            break
                        self.api.finish_session(self.session_id)
                        finish_focus = str(item[1])
                        break
                    except Exception as exc:
                        self._fail(
                            f"识别失败：{api_error_reason(exc)}",
                            code=api_error_code(exc, ErrorCode.asr_unavailable),
                        )
                        if kind != "cancel":
                            self._abandon()
                        need_drain = kind == "chunk"
                        break
        finally:
            if gate is not None:
                gate.release()
            self._session_closed.set()

        if need_drain:
            self._drain_until_end()
            return
        if finish_focus is None or self.session_id is None:
            return
        try:
            self.api.submit_utterance(
                job_id=self.job_id,
                session_id=self.session_id,
                focus_id=finish_focus,
            )
            self.outcome = "submitted"
        except Exception as exc:
            self._fail(
                f"识别提交失败：{api_error_reason(exc)}",
                code=api_error_code(exc, ErrorCode.asr_unavailable),
            )

    def _abandon(self) -> None:
        if not self.session_id:
            return
        try:
            self.api.cancel_session(self.session_id)
        except Exception:
            pass  # 会话可能已因错误关闭；结果已经以错误可见

    def _drain_until_end(self) -> None:
        while True:
            item = self._items.get()
            if item[0] in ("finish", "cancel"):
                return

    def _fail(self, message: str, *, code: ErrorCode = ErrorCode.asr_unavailable) -> None:
        self.outcome = "failed"
        print(f"[voice] {message}", flush=True)
        self.status.error(code, message)


@dataclass
class _Active:
    capture: SessionCapture
    upload: SessionUpload
    started_at: float


class VoiceFlow:
    def __init__(
        self,
        *,
        api: Any,
        status: PlatformStatus,
        probe: FocusProbe,
        source_factory: Callable[[], AudioSource],
        beep: Callable[[float], None] = lambda freq: None,
        language: Callable[[], str | None] = lambda: None,
        context_provider: Callable[[], str | None] | None = None,
        job_ids: Callable[[], str] = new_job_id,
        clock: Callable[[], float] = time.monotonic,
        prev_wait_timeout_s: float = DEFAULT_PREV_WAIT_TIMEOUT_S,
    ) -> None:
        self.api = api
        self.status = status
        self.probe = probe
        self.source_factory = source_factory
        self.beep = beep
        self.language = language
        self.context_provider = context_provider
        self.job_ids = job_ids
        self._clock = clock
        self._prev_wait_timeout_s = prev_wait_timeout_s
        self._lock = threading.Lock()
        self._session_gate = threading.Lock()
        self._active: _Active | None = None
        self.last_upload: SessionUpload | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active is not None

    def start(self) -> bool:
        """热键按下触发：立即开麦并起上传线程，绝不做阻塞网络调用。开麦失败返回 False 并可见。"""
        with self._lock:
            if self._active is not None:
                return True
            prev_upload = self.last_upload
            upload = SessionUpload(
                self.api,
                self.status,
                job_id=self.job_ids(),
                language=self.language(),
                context_provider=self.context_provider,
                prev_upload=prev_upload,
                session_gate=self._session_gate,
                prev_wait_timeout_s=self._prev_wait_timeout_s,
            )
            capture = SessionCapture(self.source_factory(), self.probe, on_chunk=upload.chunk)
            try:
                capture.start()
            except Exception as exc:
                print(f"[voice] 音频流启动失败: {exc}", flush=True)
                self.status.error(ErrorCode.dependency_failed, f"麦克风打开失败：{str(exc)[:80]}")
                return False
            self._active = _Active(capture=capture, upload=upload, started_at=self._clock())
            self.last_upload = upload
        self.beep(400)  # 低音提示: 开始录音（流启动后再播，避免设备冲突）
        upload.begin()
        return True

    def stop(self) -> None:
        """松开或看门狗 finish。"""
        with self._lock:
            active = self._active
            self._active = None
        if active is None:
            return
        result = active.capture.finish()
        self.beep(700)  # 高音提示: 结束录音
        print(f"  录音: 时长 {result.duration_s:.2f}s, {result.chunks} 块", flush=True)
        if not result.accepted:
            active.upload.cancel()
            return
        focus = result.focus.focus_id if result.focus is not None else 0
        active.upload.complete(str(focus) if focus else "")

    def stop_if_exceeded(self, max_s: float = 120.0) -> bool:
        """安全上限兜底：录音持续超过 `max_s` 视为 release 丢失，强制 finish（非 cancel）。"""
        with self._lock:
            active = self._active
            if active is None or (self._clock() - active.started_at) <= max_s:
                return False
        print(f"[voice] 录音超过 {max_s:.0f}s，判定 release 丢失，强制 finish", flush=True)
        self.stop()
        return True
