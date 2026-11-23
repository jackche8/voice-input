"""官方发行包下载：按官方校验清单比对 sha256，按扩展名解压（Ollama 与 uv 的安装动作共用）。"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import tarfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
from httpx._utils import URLPattern

from _contracts import ErrorCode, LogicError, SetupProgress
from _platform.detect import detect_os
from _platform.protocol import OsName
from _platform.system_proxy import (
    LOOPBACK_NO_PROXY,
    SystemProxyReader,
    merge_proxy_env,
    read_os_system_proxy,
    redact_url_credentials,
)

Runner = Callable[..., subprocess.CompletedProcess]
RELEASE_HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)
_NO_PROXY_SPLIT_RE = re.compile(r"[;,]")


def _extract_env_ci(env: Mapping[str, str], canon: str, *, windows: bool) -> str:
    if windows:
        exact = env.get(canon)
        if isinstance(exact, str) and exact.strip():
            return exact.strip()
        upper = canon.upper()
        for key, val in env.items():
            if key.upper() == upper and isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    for key in (canon, canon.lower()):
        val = env.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _no_proxy_patterns(token: str) -> list[str]:
    if not token or token.lower() == "<local>":
        return []
    if "://" in token:
        return [token]
    bare = token[1:-1] if (token.startswith("[") and token.endswith("]")) else token
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is not None:
        return [f"all://[{bare}]" if ip.version == 6 else f"all://{bare}"]
    if token.lower() == "localhost":
        return ["all://localhost"]
    if token.startswith("*."):
        return [f"all://{token}"]
    if token.startswith("."):
        return [f"all://*{token}"]
    if ":" in token:
        return [f"all://{token}"]
    return [f"all://{token}", f"all://*.{token}"]


def build_release_proxy_mounts(
    environ: Mapping[str, str],
    *,
    os_name: OsName | None = None,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
) -> dict[str, httpx.BaseTransport | None]:
    """将单源 `merge_proxy_env` 结果映射为 `httpx` 显式 `mounts`（避免 Windows `urllib` 注册表代理拼错）。"""
    target = os_name or detect_os()
    is_windows = target == "windows"
    proxy_env = merge_proxy_env(environ, os_name=target, read_system_proxy=read_system_proxy)

    http_proxy = _extract_env_ci(proxy_env, "HTTP_PROXY", windows=is_windows)
    https_proxy = _extract_env_ci(proxy_env, "HTTPS_PROXY", windows=is_windows)
    all_proxy = _extract_env_ci(proxy_env, "ALL_PROXY", windows=is_windows)
    eff_http = http_proxy or all_proxy
    eff_https = https_proxy or all_proxy
    if not eff_http and not eff_https:
        return {}

    no_proxy_raw = _extract_env_ci(proxy_env, "NO_PROXY", windows=is_windows)
    no_proxy_tokens = [part.strip() for part in _NO_PROXY_SPLIT_RE.split(no_proxy_raw) if part.strip()]
    if "*" in no_proxy_tokens:
        return {}

    mounts: dict[str, httpx.BaseTransport | None] = {}
    for mount_key, url in (("http://", eff_http), ("https://", eff_https)):
        if not url:
            continue
        try:
            mounts[mount_key] = httpx.HTTPTransport(proxy=url, trust_env=True)
        except ImportError as exc:
            safe_url = redact_url_credentials(url)
            raise LogicError(
                ErrorCode.dependency_failed,
                f"检测到 SOCKS 代理（{safe_url}），当前发行包下载客户端仅支持 HTTP/HTTPS 代理，请在代理软件中开启 HTTP 代理端口后重试",
            ) from exc
        except (ValueError, httpx.InvalidURL) as exc:
            safe_url = redact_url_credentials(url)
            safe_err = redact_url_credentials(str(exc))
            raise LogicError(
                ErrorCode.dependency_failed,
                f"代理地址无效（{safe_url}）：{safe_err}",
            ) from exc

    for token in (*no_proxy_tokens, *LOOPBACK_NO_PROXY):
        for pattern in _no_proxy_patterns(token):
            try:
                URLPattern(pattern)
            except Exception:
                continue
            mounts[pattern] = None
    return mounts


def create_release_client(
    *,
    environ: Mapping[str, str],
    os_name: OsName | None = None,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
    transport: httpx.BaseTransport | None = None,
    timeout: httpx.Timeout = RELEASE_HTTP_TIMEOUT,
) -> httpx.Client:
    """构造官方发行包下载客户端：有限读超时 + 显式代理 mounts（`transport` 注入时不传 mounts，绝不绕过测试桩）。"""
    if transport is not None:
        return httpx.Client(timeout=timeout, transport=transport, follow_redirects=True, trust_env=False)
    mounts = build_release_proxy_mounts(environ, os_name=os_name, read_system_proxy=read_system_proxy)
    return httpx.Client(
        timeout=timeout,
        transport=httpx.HTTPTransport(trust_env=True),
        mounts=mounts or None,
        follow_redirects=True,
        trust_env=False,
    )


def percent(done: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(min(100.0, done * 100.0 / total), 1)


def expected_sha256(sums_text: str, asset: str) -> str:
    """校验清单每行「<sha256> <文件名>」（文件名可带 * 或 ./ 前缀）。"""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == asset:
            return parts[0].lower()
    raise LogicError(ErrorCode.dependency_failed, f"官方校验清单中没有 {asset}")


def download_verified(
    client: httpx.Client,
    *,
    url: str,
    sums_url: str,
    asset: str,
    dest: Path,
    report: Callable[[SetupProgress], None],
    stage: str,
) -> None:
    """下载到 dest 并与官方清单比对；网络失败或 sha256 不一致都删掉 dest 并抛 LogicError。"""
    try:
        sums = client.get(sums_url)
        sums.raise_for_status()
        expected = expected_sha256(sums.text, asset)
        digest = hashlib.sha256()
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    report(SetupProgress(completed_bytes=done, total_bytes=total or None, percent=percent(done, total),
                                         stage=stage))
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise LogicError(ErrorCode.dependency_failed, f"下载 {asset} 失败: {exc}") from exc
    if digest.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise LogicError(ErrorCode.dependency_failed, f"{asset} 的 sha256 与官方清单不一致，已拒绝安装")


def extract_archive(archive: Path, asset: str, dest_dir: Path, *, run: Runner = subprocess.run) -> None:
    """按发行包名的扩展名解压到 dest_dir：.tar.zst 用系统 tar（标准库不支持 zstd），.tar.gz/.tgz 与 .zip 用标准库。"""
    if asset.endswith(".tar.zst"):
        proc = run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest_dir)],
                   capture_output=True, text=True, timeout=600, check=False)
        if proc.returncode != 0:
            raise LogicError(ErrorCode.dependency_failed, f"解压失败: {(proc.stderr or '').strip()[:300]}")
    elif asset.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as tar:
            tar.extractall(dest_dir, filter="data")
    elif asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    else:
        raise LogicError(ErrorCode.dependency_failed, f"不认识的发行包格式：{asset}")


__all__ = [
    "RELEASE_HTTP_TIMEOUT",
    "build_release_proxy_mounts",
    "create_release_client",
    "download_verified",
    "expected_sha256",
    "extract_archive",
    "percent",
]
