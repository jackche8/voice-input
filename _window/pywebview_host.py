"""Windows / macOS 主窗口：pywebview（WebView2 / WKWebView）。未经真机验收（D5）。

信任：Windows 在 WebView2 的 ServerCertificateErrorDetected 里只按 tls_policy.allow 放行本应用证书 + 127.0.0.1；
macOS 依赖本应用 CA 已加入登录钥匙串（_platform.certs 安装），pywebview 不提供逐证书放行的钩子，
任何证书错误都不放行。不设任何全局忽略（不使用 IGNORE_SSL_ERRORS）。
"""
from __future__ import annotations

import argparse
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

from _window import BackendMissing
from _window.commands import pump_commands
from _window.tls_policy import allow, load_cert_der

try:
    import webview
except ImportError as exc:
    raise BackendMissing(f"需要 pywebview：{exc}") from exc


def _with_route(url: str, route: str) -> str:
    parts = urlsplit(url)
    path, _, query = route.partition("?")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _install_windows_cert_handler(window, app_cert_der: bytes) -> None:
    """WebView2：证书错误时比对出示证书（DER）与本应用证书，相同且主机为 127.0.0.1 才继续，否则取消。"""
    from Microsoft.Web.WebView2.Core import CoreWebView2ServerCertificateErrorAction as Action  # pythonnet，仅 Windows

    core = window.native.webview.CoreWebView2

    def on_error(_sender, event) -> None:
        host = urlsplit(str(event.RequestUri)).hostname or ""
        presented = bytes(event.ServerCertificate.ToX509Certificate().GetRawCertData())
        event.Action = Action.AlwaysAllow if allow(host, presented, app_cert_der) else Action.Cancel

    core.ServerCertificateErrorDetected += on_error


def run(args: argparse.Namespace) -> int:
    app_cert_der = load_cert_der(args.cert)
    window = webview.create_window(args.title, args.url, width=1180, height=820)

    def started() -> None:
        if sys.platform == "win32":
            _install_windows_cert_handler(window, app_cert_der)
        threading.Thread(
            target=lambda: pump_commands(sys.stdin, lambda r: (window.load_url(_with_route(args.url, r)), window.show()),
                                         window.destroy),
            name="window-stdin", daemon=True,
        ).start()

    webview.start(started)
    return 0


__all__ = ["run"]
