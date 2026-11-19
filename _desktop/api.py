"""本机产品 API 客户端（守护进程用）。只访问回环 https，校验项目 CA/证书，禁止 verify=False 与代理劫持。"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from _contracts import SIDECAR_CHUNK_HEADER, ErrorCode
from _jobs_client import (
    DEFAULT_BASE_URL,
    DEFAULT_CERT,
    JobsClient,
    build_tls_context,
    resolve_verify_ca,
)


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def api_error_reason(exc: Exception) -> str:
    """提取异常可读原因：ApiError 取服务端 message，其他异常截断 120 字符。"""
    if isinstance(exc, ApiError):
        return exc.message
    return str(exc)[:120] or type(exc).__name__


def api_error_code(exc: Exception, default: ErrorCode) -> ErrorCode:
    """将 ApiError.code 映射为合法 ErrorCode；非 ApiError 或未知错误码回退 default。"""
    if isinstance(exc, ApiError):
        try:
            return ErrorCode(exc.code)
        except ValueError:
            return default
    return default


def _check(resp: httpx.Response) -> Any:
    if resp.status_code < 400:
        return resp.json() if resp.content else None
    try:
        body = resp.json()
        code, message = str(body.get("error", "internal")), str(body.get("message", resp.text[:200]))
    except Exception:
        code, message = "internal", resp.text[:200]
    raise ApiError(resp.status_code, code, message)


class DesktopApi:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cert: Path | None = DEFAULT_CERT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not (base_url.startswith("https://127.0.0.1") or base_url.startswith("https://localhost")):
            raise ValueError("DesktopApi 只允许本机 https")
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": httpx.Timeout(10.0, read=130.0),
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = build_tls_context(cert)
        self._client = httpx.Client(**kwargs)
        self.jobs = JobsClient(base_url, client=self._client)
        self._streams_lock = threading.Lock()
        self._active_streams: set[httpx.Response] = set()

    def close(self) -> None:
        with self._streams_lock:
            streams = list(self._active_streams)
            self._active_streams.clear()
        for resp in streams:
            try:
                resp.close()
            except Exception:
                pass
        self._client.close()

    def __enter__(self) -> "DesktopApi":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 识别会话 -------------------------------------------------------
    def create_session(
        self,
        *,
        language: str | None = None,
        context: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"language": language}
        cleaned_ctx = (context or "").strip()
        if cleaned_ctx:
            payload["context"] = cleaned_ctx
        body = _check(self._client.post("/api/v1/recognize/sessions", json=payload))
        return str(body["session_id"])

    def upload_chunk(self, session_id: str, index: int, pcm: bytes) -> None:
        _check(
            self._client.post(
                f"/api/v1/recognize/sessions/{session_id}/chunks",
                content=pcm,
                headers={
                    SIDECAR_CHUNK_HEADER: str(index),
                    "content-type": "application/octet-stream",
                },
            )
        )

    def finish_session(self, session_id: str) -> dict:
        return _check(self._client.post(f"/api/v1/recognize/sessions/{session_id}/finish"))

    def cancel_session(self, session_id: str) -> dict:
        return _check(self._client.post(f"/api/v1/recognize/sessions/{session_id}/cancel"))

    # ---- utterance ------------------------------------------------------
    def submit_utterance(self, *, job_id: str, session_id: str, focus_id: str) -> dict:
        return _check(
            self._client.post(
                "/api/v1/utterances",
                json={
                    "job_id": job_id,
                    "session_id": session_id,
                    "source": "desktop",
                    "focus_id": focus_id,
                },
            )
        )

    def resend(
        self,
        *,
        job_id: str,
        text: str,
        original_job_id: str,
        focus_id: str,
        confirm: bool,
    ) -> dict:
        return _check(
            self._client.post(
                "/api/v1/utterances/resend",
                json={
                    "job_id": job_id,
                    "text": text,
                    "original_job_id": original_job_id,
                    "focus_id": focus_id,
                    "confirm": confirm,
                },
            )
        )

    # ---- 原位 OCR / 翻译 ------------------------------------------------
    def ocr_inplace(
        self,
        png_bytes: bytes,
        *,
        target_lang: str,
        lang: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"target_lang": target_lang}
        if lang:
            data["lang"] = lang
        files = {"image": ("capture.png", png_bytes, "image/png")}
        return _check(
            self._client.post(
                "/api/v1/ocr-inplace",
                data=data,
                files=files,
                timeout=httpx.Timeout(10.0, read=130.0),
            )
        )

    # ---- 产品事件流（SSE）-----------------------------------------------
    def iter_product_events(
        self,
        stop_event: threading.Event | None = None,
        *,
        read_timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式消费 `GET /api/v1/events` SSE 并按条产出事件字典。"""
        timeout = httpx.Timeout(10.0, read=read_timeout)
        with self._client.stream(
            "GET",
            "/api/v1/events",
            headers={"Accept": "text/event-stream"},
            timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _check(resp)
            with self._streams_lock:
                self._active_streams.add(resp)
            try:
                event_name: str | None = None
                data_lines: list[str] = []
                for line in resp.iter_lines():
                    if stop_event is not None and stop_event.is_set():
                        break
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            data_lines.clear()
                            payload = _parse_sse_payload(raw, event_name)
                            event_name = None
                            if payload is not None:
                                yield payload
                        else:
                            event_name = None
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[6:] if line.startswith("data: ") else line[5:])
            finally:
                with self._streams_lock:
                    self._active_streams.discard(resp)

    # ---- 平台状态 & 配置 ------------------------------------------------
    def post_platform_event(self, event: dict) -> None:
        _check(self._client.post("/api/v1/events/platform", json=event))

    def get_config(self) -> dict:
        return _check(self._client.get("/api/v1/config"))


def _parse_sse_payload(raw: str, event_name: str | None) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if event_name and "event" not in parsed:
        parsed["event"] = event_name
    return parsed


__all__ = ["ApiError", "DesktopApi", "resolve_verify_ca"]
