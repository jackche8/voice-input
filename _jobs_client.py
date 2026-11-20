"""本机 jobs HTTP 客户端。只 claim/ack/查询，不重做业务决策。

必须显式给出本应用证书（`_paths.AppPaths.cert`）以校验 CA，禁止 verify=False，禁用系统代理劫持回环（trust_env=False）。
"""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx

from _inject import AckResult, ClaimAckLog
from _ports import DEFAULT_SERVER_PORT

DEFAULT_BASE_URL = f"https://127.0.0.1:{DEFAULT_SERVER_PORT}"


def resolve_verify_ca(cert: Path | str | None) -> Path:
    """解析用于验证本机 HTTPS 证书链的 CA 文件：同目录存在 `ca.pem` 时优先使用，否则回退传入证书。"""
    if cert is None:
        raise ValueError("缺少项目证书，拒绝关闭 TLS 校验")
    cert_path = Path(cert)
    ca_path = cert_path.with_name("ca.pem")
    if ca_path.is_file():
        return ca_path
    if cert_path.is_file():
        return cert_path
    raise ValueError("缺少项目证书，拒绝关闭 TLS 校验")


_resolve_verify_ca = resolve_verify_ca


def build_tls_context(cert: Path | str | None) -> ssl.SSLContext:
    """构建本机回环 HTTPS 客户端用的严格 TLS 上下文。"""
    ca_file = resolve_verify_ca(cert)
    return ssl.create_default_context(cafile=str(ca_file))


class JobsClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cert: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        acks: ClaimAckLog | None = None,
    ) -> None:
        allowed = (
            base_url.startswith("https://127.0.0.1")
            or base_url.startswith("https://localhost")
            or base_url.startswith("http://127.0.0.1")
        )
        if not allowed:
            raise ValueError("JobsClient 只允许回环地址")
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "timeout": 30.0,
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        elif client is None:
            kwargs["verify"] = build_tls_context(cert)
        self._owns = client is None
        self._client = client or httpx.Client(**kwargs)
        self.acks = acks or ClaimAckLog()

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "JobsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def claim(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        resp = self._client.post("/api/v1/jobs/claim", json=payload or None)
        resp.raise_for_status()
        return resp.json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    def ack(
        self,
        job_id: str,
        claim_id: str,
        result: AckResult,
        *,
        detail: str | None = None,
    ) -> dict[str, Any]:
        self.acks.record(claim_id, result)
        payload: dict[str, Any] = {"claim_id": claim_id, "result": result}
        if detail:
            payload["detail"] = detail
        resp = self._client.post(f"/api/v1/jobs/{job_id}/ack", json=payload)
        resp.raise_for_status()
        return resp.json()
