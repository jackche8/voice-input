"""设置页的真实动作（生产入口注入；测试只用假传输/假进程，不触网、不调 live systemctl）。

- ollama_pull：Ollama 官方 POST /api/pull 流式进度（https://docs.ollama.com/api/pull）。
- ollama_install：本平台官方发行包（ollama_asset）+ 官方 sha256sum.txt 校验，解压到用户目录，
  不用 sudo、不写系统目录（https://docs.ollama.com/linux 手动安装流程的用户级变体）。
- asr_download：在隔离 ASR 环境里用官方 huggingface_hub snapshot_download 下载 Qwen3-ASR 权重。
每个动作失败都抛 LogicError，成功前必须复查真实状态。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from _contracts import DEFAULT_OLLAMA_BASE_URL, ErrorCode, LogicError, SetupProgress
from _platform.child_process import windows_subprocess_kwargs
from _v3.release_download import download_verified, extract_archive, percent
from _v3.setup_probe import probe_asr_weights
from _v3.setup_tasks import ProgressFn, SetupAction

Runner = Callable[..., subprocess.CompletedProcess]
OLLAMA_RELEASE_BASE = "https://github.com/ollama/ollama/releases/download"
_OLLAMA_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
FAKEIP_RETRY_STAGE = "检测到代理 Fake-IP 拦截，改用兼容模式重试拉取"

_GO_HTTP_PREFIX_RE = re.compile(r'(?i)\b(?:Get|Head|Post|Put|Delete)\s+"https?://[^"]+"\s*:\s*')
_BARE_URL_RE = re.compile(r"https?://\S+")
_MAX_RETRIES_RE = re.compile(r"(?i)\bmax retries exceeded:\s*")
_MAX_UNKNOWN_DETAIL_CHARS = 160


class _PullStreamFailure(Exception):
    def __init__(self, raw: str, *, status_code: int | None = None) -> None:
        super().__init__(raw)
        self.raw = raw
        self.status_code = status_code


def _extract_host_port(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" in text:
        return urlsplit(text).netloc.strip()
    return text.strip("/")


def _resolve_ollama_host(ollama_host: str | None, client: httpx.Client) -> str:
    raw_netloc = getattr(client.base_url, "netloc", b"")
    client_netloc = raw_netloc.decode("ascii", "ignore") if isinstance(raw_netloc, bytes) else str(raw_netloc or "")
    for candidate in (ollama_host, client_netloc, DEFAULT_OLLAMA_BASE_URL):
        resolved = _extract_host_port(candidate)
        if resolved:
            return resolved
    return urlsplit(DEFAULT_OLLAMA_BASE_URL).netloc


def _extract_http_error_text(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"].strip():
        return data["error"].strip()
    return body.strip()


def _log_ollama_pull_error(raw: str, *, model: str, attempt: int, status_code: int | None = None) -> None:
    if sys.stderr is None:
        return
    suffix = f" http={status_code}" if status_code is not None else ""
    print(f"[ollama_pull] model={model} attempt={attempt}{suffix} raw_error={raw}", file=sys.stderr, flush=True)


def is_ollama_fakeip_redirect_error(raw: str) -> bool:
    """判定报错是否明确命中 Ollama 0.40 的 Fake-IP / 非公网地址重定向拦截。"""
    low = (raw or "").lower()
    return "redirect target not allowed" in low and (
        "resolves to non-public" in low or "is not a public address" in low
    )


def _sanitize_ollama_pull_error(raw: str) -> str:
    text = _GO_HTTP_PREFIX_RE.sub("", raw or "")
    text = _BARE_URL_RE.sub("", text)
    text = _MAX_RETRIES_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" :：")
    if len(text) > _MAX_UNKNOWN_DETAIL_CHARS:
        text = text[: _MAX_UNKNOWN_DETAIL_CHARS - 1].rstrip() + "…"
    return text


def _classify_ollama_pull_reason(raw: str, sanitized: str, *, status_code: int | None) -> tuple[str, str]:
    low = (raw or "").lower()
    if raw.startswith("无法连接") or "无法连接本地 ollama" in low:
        brief, cat = "无法连接本地 Ollama 服务", "connect"
    elif is_ollama_fakeip_redirect_error(raw):
        brief, cat = "代理 Fake-IP 非公网地址重定向被拦截", "fakeip"
    elif (
        "proxyconnect" in low
        or "bad gateway" in low
        or ("proxy" in low and any(k in low for k in ("error", "fail", "refused", "forbidden", "timeout", "eof")))
    ):
        brief, cat = "代理连接失败", "network"
    elif any(k in low for k in ("deadline exceeded", "timed out", "timeout")):
        brief, cat = "连接或下载超时", "network"
    elif any(k in low for k in ("x509:", "tls:", "certificate")):
        brief, cat = "TLS 证书校验失败", "network"
    elif any(k in low for k in ("no such host", "lookup ", "name or service not known")):
        brief, cat = "域名解析失败", "network"
    elif any(k in low for k in ("no space left on device", "disk full", "not enough space")):
        brief, cat = "磁盘空间不足", "disk"
    elif "412" in low and (any(k in low for k in ("manifest", "precondition", "version")) or status_code == 412):
        brief, cat = "Ollama 版本过旧或清单校验失败（412）", "version"
    elif (
        any(k in low for k in ("connection refused", "connection reset", "unexpected eof", "broken pipe", "dial tcp"))
        or re.search(r"\beof\b", low) is not None
    ):
        brief, cat = "网络连接中断或被拒绝", "network"
    else:
        brief, cat = sanitized or "未知错误", "generic"

    if status_code is not None and f"http {status_code}" not in brief.lower():
        brief = f"HTTP {status_code}: {brief}"
    return brief, cat


def format_ollama_pull_error(
    raw: str,
    *,
    model: str,
    ollama_host: str,
    status_code: int | None = None,
    after_fakeip_retry: bool = False,
) -> str:
    """单源整理 Ollama 拉取报错：剥离长签名 URL 与重试栈，输出简短中文原因与可执行指引。"""
    host = _extract_host_port(ollama_host) or urlsplit(DEFAULT_OLLAMA_BASE_URL).netloc
    sanitized = _sanitize_ollama_pull_error(raw)
    brief, cat = _classify_ollama_pull_reason(raw, sanitized, status_code=status_code)
    if after_fakeip_retry:
        return (
            f"检测到代理 Fake-IP 拦截，兼容模式重试仍失败（{brief}）。"
            f"请在代理/旁路由中放行 ollama.com 与 r2.cloudflarestorage.com（跳过 Fake-IP），"
            f"或手动执行：OLLAMA_HOST={host} ollama pull --insecure {model}"
        )
    if cat == "connect":
        return f"无法连接 Ollama（请确认本地 Ollama 服务正在运行）。也可手动执行：OLLAMA_HOST={host} ollama pull {model}"
    if cat == "disk":
        return f"模型拉取失败：{brief}。请清理磁盘空间后重试，或手动执行：OLLAMA_HOST={host} ollama pull {model}"
    if cat == "version":
        return f"模型拉取失败：{brief}。请升级 Ollama 后重试，或手动执行：OLLAMA_HOST={host} ollama pull {model}"
    return f"模型拉取失败：{brief}。请检查网络或代理设置后重试，或手动执行：OLLAMA_HOST={host} ollama pull {model}"


def _pull_stream_once(client: httpx.Client, payload: dict[str, object], report: ProgressFn) -> None:
    layers: dict[str, tuple[int, int]] = {}
    with client.stream("POST", "/api/pull", json=payload) as resp:
        if resp.status_code >= 400:
            body = resp.read().decode("utf-8", "replace")
            raise _PullStreamFailure(_extract_http_error_text(body), status_code=resp.status_code)
        last_status = ""
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _PullStreamFailure(f"Ollama 拉取进度无法解析: {line}") from exc
            if item.get("error"):
                raise _PullStreamFailure(str(item["error"]))
            last_status = str(item.get("status") or last_status)
            digest = item.get("digest")
            if digest and item.get("total"):
                layers[digest] = (int(item.get("completed") or 0), int(item["total"]))
            done = sum(c for c, _ in layers.values())
            total = sum(t for _, t in layers.values())
            report(
                SetupProgress(
                    completed_bytes=done or None,
                    total_bytes=total or None,
                    percent=percent(done, total),
                    stage=last_status,
                )
            )
    if last_status != "success":
        raise _PullStreamFailure(f"Ollama 拉取未完成，最后状态: {last_status or '无'}")


def ollama_asset(os_name: str, machine: str) -> str:
    """官方发行包名：Linux tar.zst（bin/ollama）、Windows zip（ollama.exe）、macOS zip（Ollama.app）。"""
    if os_name == "macos":
        return "Ollama-darwin.zip"
    arch = _OLLAMA_ARCH.get(machine.lower())
    if arch is None:
        raise LogicError(ErrorCode.dependency_failed, f"Ollama 没有 {machine} 架构的官方发行包")
    return f"ollama-windows-{arch}.zip" if os_name == "windows" else f"ollama-linux-{arch}.tar.zst"


def ollama_pull_action(
    *,
    client_factory: Callable[[], httpx.Client],
    model: str,
    ollama_host: str | None = None,
) -> SetupAction:
    def run(report: ProgressFn) -> str:
        with client_factory() as client:
            host = _resolve_ollama_host(ollama_host, client)
            retried = False
            try:
                _pull_stream_once(client, {"model": model, "stream": True}, report)
            except _PullStreamFailure as first_err:
                _log_ollama_pull_error(first_err.raw, model=model, attempt=1, status_code=first_err.status_code)
                if not is_ollama_fakeip_redirect_error(first_err.raw):
                    raise LogicError(
                        ErrorCode.dependency_failed,
                        format_ollama_pull_error(
                            first_err.raw, model=model, ollama_host=host, status_code=first_err.status_code
                        ),
                    ) from None
                retried = True
                report(SetupProgress(completed_bytes=None, total_bytes=None, percent=None, stage=FAKEIP_RETRY_STAGE))
                try:
                    _pull_stream_once(client, {"model": model, "stream": True, "insecure": True}, report)
                except _PullStreamFailure as retry_err:
                    _log_ollama_pull_error(retry_err.raw, model=model, attempt=2, status_code=retry_err.status_code)
                    raise LogicError(
                        ErrorCode.dependency_failed,
                        format_ollama_pull_error(
                            retry_err.raw,
                            model=model,
                            ollama_host=host,
                            status_code=retry_err.status_code,
                            after_fakeip_retry=True,
                        ),
                    ) from None
                except httpx.HTTPError as exc:
                    raw = f"无法连接本地 Ollama 服务: {exc}"
                    _log_ollama_pull_error(raw, model=model, attempt=2)
                    raise LogicError(
                        ErrorCode.dependency_failed,
                        format_ollama_pull_error(raw, model=model, ollama_host=host, after_fakeip_retry=True),
                    ) from exc
            except httpx.HTTPError as exc:
                raw = f"无法连接本地 Ollama 服务: {exc}"
                _log_ollama_pull_error(raw, model=model, attempt=1)
                raise LogicError(
                    ErrorCode.dependency_failed,
                    format_ollama_pull_error(raw, model=model, ollama_host=host),
                ) from exc

            try:
                tags = client.get("/api/tags")
                tags.raise_for_status()
                names = {m.get("name") for m in tags.json().get("models") or []} | {
                    m.get("model") for m in tags.json().get("models") or []
                }
            except httpx.HTTPError as exc:
                raw = f"无法连接本地 Ollama 服务: {exc}"
                _log_ollama_pull_error(raw, model=model, attempt=2 if retried else 1)
                raise LogicError(
                    ErrorCode.dependency_failed,
                    format_ollama_pull_error(raw, model=model, ollama_host=host, after_fakeip_retry=retried),
                ) from exc
        if model not in names:
            raise LogicError(ErrorCode.dependency_failed, f"拉取结束但 /api/tags 中没有 {model}")
        return f"{model} 已下载"

    return run


def _ollama_partials(install_dir: Path) -> tuple[Path, Path]:
    return install_dir.with_name(f".{install_dir.name}.download"), install_dir.with_name(f".{install_dir.name}.staging")


def _remove_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _remove_existing_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def ollama_install_cleanup(install_dir: Path) -> None:
    """删除下载文件与解压目录（安装结束时、或重试前清理上次中断的半成品；不碰已安装好的 install_dir）。"""
    download, staging = _ollama_partials(install_dir)
    download.unlink(missing_ok=True)
    _remove_dir(staging)


def ollama_install_action(
    *,
    version: str,
    install_dir: Path,
    binary: Path,
    asset: str,
    client_factory: Callable[[], httpx.Client],
    run: Runner = subprocess.run,
) -> SetupAction:
    """下载本平台的官方发行包到用户目录（不用 sudo、不写系统目录）；解压后 binary 必须存在。"""
    binary_rel = binary.relative_to(install_dir)

    def install(report: ProgressFn) -> str:
        if binary.is_file():
            raise LogicError(ErrorCode.conflict, f"{install_dir} 已有 Ollama，未重复安装")
        base = f"{OLLAMA_RELEASE_BASE}/v{version}"
        download, staging = _ollama_partials(install_dir)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            with client_factory() as client:
                download_verified(client, url=f"{base}/{asset}", sums_url=f"{base}/sha256sum.txt", asset=asset,
                                  dest=download, report=report, stage="下载 Ollama 官方发行包")
            report(SetupProgress(stage="解压到用户目录"))
            _remove_dir(staging)
            staging.mkdir(parents=True)
            extract_archive(download, asset, staging, run=run)
            if not (staging / binary_rel).is_file():
                raise LogicError(ErrorCode.dependency_failed, f"发行包 {asset} 内没有 {binary_rel}")
            if install_dir.exists() or install_dir.is_symlink():
                _remove_existing_path(install_dir)
            os.replace(staging, install_dir)
        finally:
            ollama_install_cleanup(install_dir)
        return f"Ollama {version} 已安装到 {install_dir}"

    return install


def asr_download_action(
    *,
    python: Path,
    worker: Path,
    repo_id: str,
    model_dir: Path,
    log_path: Path,
    env: dict[str, str],
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SetupAction:
    def download(report: ProgressFn) -> str:
        if not python.is_file():
            raise LogicError(ErrorCode.dependency_failed, f"识别运行环境未安装: {python}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total = {"bytes": 0}
        with log_path.open("w", encoding="utf-8") as log:
            proc = popen(
                [str(python), str(worker), repo_id, str(model_dir)],
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                env={**env, "PYTHONUTF8": "1"},
                **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
            )

            def read_stdout() -> None:
                for line in proc.stdout:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "total_bytes" in item:
                        total["bytes"] = int(item["total_bytes"])

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            while proc.poll() is None:
                done = probe_asr_weights(model_dir).bytes_on_disk if model_dir.exists() else 0
                report(SetupProgress(completed_bytes=done, total_bytes=total["bytes"] or None,
                                     percent=percent(done, total["bytes"]), stage="下载识别模型权重"))
                sleep(interval_s)
            reader.join(timeout=5)
        if proc.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            raise LogicError(ErrorCode.dependency_failed, f"识别模型下载失败（退出码 {proc.returncode}）: {tail}")
        probe = probe_asr_weights(model_dir)
        if not probe.complete:
            raise LogicError(ErrorCode.dependency_failed, "下载进程结束但权重不完整: " + "、".join(probe.missing))
        return f"{repo_id} 已下载"

    return download
