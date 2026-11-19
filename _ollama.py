"""Ollama HTTP 客户端（/api/chat，think:false）。供 _polish 与 _translate 复用，避免两套实现。

真实复用理由：纠错和翻译都要走同一官方 chat 接口、同一超时/JSON/失败可见处理，
以及强制 think=false、回环禁代理（trust_env=False）；本模块不含业务提示词，只负责协议。
"""
from __future__ import annotations

import inspect
import re
from typing import Any

import httpx

from _contracts import DEFAULT_OLLAMA_BASE_URL, ErrorCode, LogicError

DEFAULT_TIMEOUT_S = 120.0

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*$", flags=re.DOTALL | re.IGNORECASE)


def strip_think_and_wrapper(raw_text: str, wrapper_tag: str | None = None) -> str:
    """剥离模型可能残留的 <think>...</think> 块以及回显的边界包裹标签。"""
    cleaned = _THINK_BLOCK_RE.sub("", raw_text or "")
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned).strip()
    if wrapper_tag:
        open_pat = re.compile(rf"^\s*<{re.escape(wrapper_tag)}>\s*", flags=re.IGNORECASE)
        close_pat = re.compile(rf"\s*</{re.escape(wrapper_tag)}>\s*$", flags=re.IGNORECASE)
        cleaned = open_pat.sub("", cleaned)
        cleaned = close_pat.sub("", cleaned)
    return cleaned.strip()


def call_chat(
    client_obj: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any] | None = None,
) -> str:
    """调用 OllamaClient 或测试注入替身的 chat()，自动兼容 2 参数替身签名。"""
    chat_fn = client_obj.chat
    try:
        sig = inspect.signature(chat_fn)
    except (TypeError, ValueError):
        return chat_fn(model=model, messages=messages, think=False, options=options)

    params = sig.parameters
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if not has_var_kw and "model" not in params and "messages" not in params:
        pos_params = [
            p
            for p in params.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())
        if has_var_pos or len(pos_params) >= 2:
            return chat_fn(model, messages)

    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if has_var_kw or "think" in params:
        kwargs["think"] = False
    if options is not None and (has_var_kw or "options" in params):
        kwargs["options"] = options
    return chat_fn(**kwargs)


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool = False,
        options: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """调用 /api/chat。默认且产品要求 think=false。

        失败（超时、非 2xx、非法 JSON、空内容）必须抛 LogicError，
        调用方不得改把原文当成成功返回。
        """
        if think:
            raise LogicError(
                ErrorCode.invalid_request,
                "Ollama chat 必须使用 think=false，禁止开启 think",
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
        }
        if extra:
            payload.update(extra)
        if options is not None:
            merged_opts = dict(payload.get("options") or {})
            merged_opts.update(options)
            payload["options"] = merged_opts
        payload["think"] = False
        payload["stream"] = False

        url = "/api/chat"
        try:
            resp = self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise LogicError(ErrorCode.timeout, f"Ollama 请求超时: {exc}") from exc
        except httpx.RequestError as exc:
            raise LogicError(
                ErrorCode.dependency_failed, f"无法连接 Ollama: {exc}"
            ) from exc
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300]
            err_msg = f"Ollama 返回 HTTP {resp.status_code}"
            if resp.status_code == 404:
                err_detail = ""
                try:
                    err_body = resp.json()
                    if isinstance(err_body, dict):
                        err_detail = str(err_body.get("error") or "")
                except ValueError:
                    err_detail = snippet
                low_detail = (err_detail or snippet).lower()
                if "model" in low_detail and "not found" in low_detail:
                    err_msg = f"Ollama 模型 {model} 尚未下载（请先在 /setup 下载模型，HTTP 404）"
            raise LogicError(
                ErrorCode.dependency_failed,
                err_msg,
                details={"status_code": resp.status_code, "body": snippet},
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LogicError(
                ErrorCode.dependency_failed, f"Ollama 响应不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应根节点必须是对象")
        message = data.get("message")
        if not isinstance(message, dict):
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应缺少 message 对象")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LogicError(ErrorCode.dependency_failed, "Ollama 响应缺少文本内容")
        return content.strip()


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "OllamaClient",
    "call_chat",
    "strip_think_and_wrapper",
]
