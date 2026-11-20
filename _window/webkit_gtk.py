"""Linux 主窗口：gi 的 GTK 3 + WebKit2GTK 4.1。

信任：只把本应用服务端证书登记为 127.0.0.1 的例外（WebContext.allow_tls_certificate_for_host，逐证书比对），
不设任何全局忽略；其它主机或其它证书仍按系统规则拒绝。stdin 命令在读线程里解析，经 GLib.idle_add 回 GTK 主线程。
"""
from __future__ import annotations

import argparse
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

from _window import BackendMissing
from _window.commands import pump_commands
from _window.tls_policy import TRUSTED_HOST

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as exc:  # 没装 gi 或没有 WebKit2GTK 4.1
    raise BackendMissing(f"需要 gi 的 WebKit2GTK 4.1（gir1.2-webkit2-4.1）：{type(exc).__name__}: {exc}") from exc


def _with_route(url: str, route: str) -> str:
    parts = urlsplit(url)
    path, _, query = route.partition("?")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def run(args: argparse.Namespace) -> int:
    context = WebKit2.WebContext.get_default()
    context.allow_tls_certificate_for_host(Gio.TlsCertificate.new_from_file(args.cert), TRUSTED_HOST)
    view = WebKit2.WebView.new_with_context(context)
    window = Gtk.Window(title=args.title)
    window.set_default_size(1180, 820)
    window.add(view)
    window.connect("destroy", lambda *_: Gtk.main_quit())

    def tls_failed(_view, failing_uri, _certificate, errors):
        print(f"主界面证书校验失败：{failing_uri}（{errors}）", file=sys.stderr, flush=True)
        return False  # 不放行：交给 WebKit 显示错误页

    view.connect("load-failed-with-tls-errors", tls_failed)

    def show(route: str) -> bool:
        view.load_uri(_with_route(args.url, route))
        window.present()
        return False

    def quit_() -> bool:
        window.destroy()
        return False

    reader = threading.Thread(
        target=lambda: pump_commands(sys.stdin, lambda r: GLib.idle_add(show, r), lambda: GLib.idle_add(quit_)),
        name="window-stdin", daemon=True,
    )
    view.load_uri(args.url)
    window.show_all()
    reader.start()
    Gtk.main()
    return 0


__all__ = ["run"]
