"""桌面主窗口外链判定与系统浏览器拉起（纯标准库，无 `gi` / `webview` 依赖）。"""
from __future__ import annotations

import webbrowser
from collections.abc import Callable
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _http_origin(uri: str) -> tuple[str, str, int] | None:
    if not isinstance(uri, str):
        return None
    text = uri.strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            return None
        host = (parts.hostname or "").lower()
        if not host:
            return None
        port = parts.port if parts.port is not None else _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, host, port


def should_open_externally(uri: str, app_origin: str) -> bool:
    """仅当 `uri` 为有效 http/https 地址且源（scheme/host/port）不同于本应用源时返回 True。"""
    target = _http_origin(uri)
    if target is None:
        return False
    origin = _http_origin(app_origin)
    return origin is None or target != origin


def open_external_url(uri: str, *, opener: Callable[[str], object] = webbrowser.open) -> bool:
    """校验为合法 http/https 地址后交由系统默认浏览器打开，失败不抛异常中断主窗口循环。"""
    if _http_origin(uri) is None:
        return False
    try:
        return bool(opener(uri.strip()))
    except Exception:  # noqa: BLE001 — 系统浏览器调用失败不得崩主窗口
        return False


__all__ = ["open_external_url", "should_open_externally"]
