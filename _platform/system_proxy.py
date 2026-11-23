"""跨平台 OS 系统代理读取与 Go `net/http` 兼容环境变量合并（逻辑层）。

Go 标准库 `net/http.ProxyFromEnvironment` 仅读取环境变量，不会自动读取 Windows 注册表
`Internet Settings` 或 macOS SystemConfiguration；本模块在启动 `ollama serve` 时按平台安全同步
系统代理并保底直连回环（`127.0.0.1,localhost,::1`），读取异常 fail-open 绝不阻断服务启动。
"""
from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from _platform.detect import detect_os
from _platform.protocol import OsName

WINDOWS_INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
GO_SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
LOOPBACK_NO_PROXY = ("127.0.0.1", "localhost", "::1")

_NO_PROXY_SPLIT_RE = re.compile(r"[;,]")
_INVALID_HOST_CHARS = frozenset({"=", ";", ",", "/", "\\"})

SystemProxyReader = Callable[[OsName], tuple[dict[str, str], list[str]]]


def normalize_go_proxy_url(raw: str, *, default_scheme: str | None = "http") -> str | None:
    """校验并规范化代理 URL：仅放行 Go `net/http` 支持的 http/https/socks5/socks5h，拒绝 socks4 与畸形值。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    if "://" not in text:
        if default_scheme is None:
            return None
        candidate = f"{default_scheme}://{text}"
    else:
        candidate = text
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in GO_SUPPORTED_PROXY_SCHEMES:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname
    if not host or any(ch in _INVALID_HOST_CHARS for ch in host):
        return None
    # Python urlsplit 对尾冒号空端口（如 http://127.0.0.1: 或 http://[::1]:）返回 port=None 而非抛错，须显式拒绝
    hostport = parsed.netloc.rsplit("@", 1)[-1]
    if hostport.endswith(":"):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    return f"{scheme}://{parsed.netloc}"


def _parse_no_proxy_tokens(sources: Iterable[str | None]) -> list[str]:
    """按逗号/分号拆分并去重（大小写不敏感保序），同时剔除 Windows 专用 `<local>` 占位符。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in sources:
        if not isinstance(raw, str) or not raw:
            continue
        for part in _NO_PROXY_SPLIT_RE.split(raw):
            token = part.strip()
            if not token:
                continue
            key = token.lower()
            if key == "<local>" or key in seen:
                continue
            seen.add(key)
            out.append(token)
    return out


def _parse_windows_proxy_server(proxy_server: str) -> dict[str, str]:
    """解析 Windows 注册表 `ProxyServer`（支持单值、尾分号与 `http=...;https=...` 分协议格式）。"""
    segments = [seg.strip() for seg in proxy_server.split(";") if seg.strip()]
    if not segments:
        return {}
    if not any("=" in seg for seg in segments):
        if len(segments) != 1:
            return {}
        url = normalize_go_proxy_url(segments[0], default_scheme="http")
        return {"http": url, "https": url} if url else {}

    proxies: dict[str, str] = {}
    fallback_socks: str | None = None
    for seg in segments:
        if "=" not in seg:
            continue
        proto, addr = seg.split("=", 1)
        proto = proto.strip().lower()
        addr = addr.strip()
        if proto == "http":
            url = normalize_go_proxy_url(addr, default_scheme="http")
            if url:
                proxies["http"] = url
        elif proto == "https":
            # Windows `https=127.0.0.1:7890` 指明经 HTTP CONNECT 代理转发 HTTPS 流量，裸地址默认补 http://
            url = normalize_go_proxy_url(addr, default_scheme="http")
            if url:
                proxies["https"] = url
        elif proto in ("socks5", "socks5h"):
            url = normalize_go_proxy_url(addr, default_scheme=proto)
            if url:
                fallback_socks = url
        elif proto == "socks":
            # WinINet `socks=host:port` 默认表示 SOCKS4（Go 不支持），仅放行已显式声明支持协议的地址
            url = normalize_go_proxy_url(addr, default_scheme=None)
            if url:
                fallback_socks = url
    if fallback_socks:
        proxies.setdefault("http", fallback_socks)
        proxies.setdefault("https", fallback_socks)
    return proxies


def read_windows_system_proxy(*, reg_api: Any = None) -> tuple[dict[str, str], list[str]]:
    """读取当前用户 Windows 注册表 `Internet Settings` 代理配置；异常或未启用时 fail-open 返回空。"""
    try:
        reg = reg_api
        if reg is None:
            import winreg as reg  # type: ignore[import-not-found]
        with reg.OpenKey(reg.HKEY_CURRENT_USER, WINDOWS_INTERNET_SETTINGS_KEY) as key:
            proxy_enable = reg.QueryValueEx(key, "ProxyEnable")[0]
            if not proxy_enable:
                return {}, []
            raw_server = str(reg.QueryValueEx(key, "ProxyServer")[0] or "")
            try:
                raw_override = str(reg.QueryValueEx(key, "ProxyOverride")[0] or "")
            except OSError:
                raw_override = ""
    except Exception:
        return {}, []
    proxies = _parse_windows_proxy_server(raw_server)
    overrides = _parse_no_proxy_tokens([raw_override]) if proxies else []
    return proxies, overrides


def _read_macos_system_proxy(*, macos_reader: Callable[[], Mapping[str, str]] | None = None) -> tuple[dict[str, str], list[str]]:
    reader = macos_reader if macos_reader is not None else getattr(urllib.request, "getproxies_macosx_sysconf", None)
    if reader is None:
        return {}, []
    try:
        raw_map = reader()
    except Exception:
        return {}, []
    if not isinstance(raw_map, Mapping):
        return {}, []
    proxies: dict[str, str] = {}
    for proto in ("http", "https"):
        raw_val = raw_map.get(proto)
        if isinstance(raw_val, str):
            url = normalize_go_proxy_url(raw_val, default_scheme=None)
            if url:
                proxies[proto] = url
    return proxies, []


def read_os_system_proxy(
    os_name: OsName | None = None,
    *,
    reg_api: Any = None,
    macos_reader: Callable[[], Mapping[str, str]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """按平台读取 OS 系统代理与绕过清单；Linux 或读取异常时 fail-open 返回 `({}, [])`。"""
    target = os_name or detect_os()
    if target == "windows":
        return read_windows_system_proxy(reg_api=reg_api)
    if target == "macos":
        return _read_macos_system_proxy(macos_reader=macos_reader)
    return {}, []


def _extract_env_proxy(env: dict[str, str], canon: str, *, windows: bool) -> str:
    """读取用户已显式设置的代理值；Windows 下归一化到唯一规范大写键，防 CreateProcessW 重复键。"""
    if windows:
        value = ""
        exact = env.get(canon)
        if isinstance(exact, str) and exact.strip():
            value = exact.strip()
        for key in list(env):
            if key.upper() != canon:
                continue
            if not value and isinstance(env[key], str) and env[key].strip():
                value = env[key].strip()
            if key != canon:
                del env[key]
        if value:
            env[canon] = value
        return value
    for key in (canon, canon.lower()):
        val = env.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def merge_proxy_env(
    environ: Mapping[str, str],
    *,
    os_name: OsName | None = None,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
) -> dict[str, str]:
    """合并用户环境变量与 OS 系统代理到子进程环境；只要最终含代理即保底直连回环。"""
    target = os_name or detect_os()
    is_windows = target == "windows"
    out = dict(environ)

    existing_http = _extract_env_proxy(out, "HTTP_PROXY", windows=is_windows)
    existing_https = _extract_env_proxy(out, "HTTPS_PROXY", windows=is_windows)

    os_overrides: list[str] = []
    if not existing_http or not existing_https:
        try:
            os_proxies, raw_overrides = read_system_proxy(target)
        except Exception:
            os_proxies, raw_overrides = {}, []
        os_overrides = list(raw_overrides or ())
        if not existing_http:
            url = normalize_go_proxy_url(os_proxies.get("http", ""), default_scheme=None)
            if url:
                out["HTTP_PROXY"] = url
                existing_http = url
        if not existing_https:
            url = normalize_go_proxy_url(os_proxies.get("https", ""), default_scheme=None)
            if url:
                out["HTTPS_PROXY"] = url
                existing_https = url

    if not (existing_http or existing_https):
        return out

    if is_windows:
        existing_no_proxy: list[str] = []
        if "NO_PROXY" in out:
            existing_no_proxy.append(out["NO_PROXY"])
        for key in list(out):
            if key.upper() == "NO_PROXY" and key != "NO_PROXY":
                existing_no_proxy.append(out.pop(key))
        merged = ",".join(_parse_no_proxy_tokens([*existing_no_proxy, *os_overrides, *LOOPBACK_NO_PROXY]))
        out["NO_PROXY"] = merged
        return out

    merged = ",".join(
        _parse_no_proxy_tokens([out.get("NO_PROXY"), out.get("no_proxy"), *os_overrides, *LOOPBACK_NO_PROXY])
    )
    out["NO_PROXY"] = merged
    if "no_proxy" in out:
        out["no_proxy"] = merged
    return out


__all__ = [
    "GO_SUPPORTED_PROXY_SCHEMES",
    "LOOPBACK_NO_PROXY",
    "SystemProxyReader",
    "WINDOWS_INTERNET_SETTINGS_KEY",
    "merge_proxy_env",
    "normalize_go_proxy_url",
    "read_os_system_proxy",
    "read_windows_system_proxy",
]
