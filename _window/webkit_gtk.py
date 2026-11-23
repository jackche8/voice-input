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
from _window.external_links import open_external_url, should_open_externally
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


def wire_external_link_handlers(view, app_origin: str, webkit_mod=None, *, opener=None) -> None:
    """拦截外链导航与新窗请求并交系统浏览器打开（使用 WebKit2GTK 4.1 规范 `decision.get_navigation_action().get_request().get_uri()`）。"""
    webkit = WebKit2 if webkit_mod is None else webkit_mod
    nav_type = webkit.PolicyDecisionType.NAVIGATION_ACTION
    new_win_type = webkit.PolicyDecisionType.NEW_WINDOW_ACTION

    def _open(uri: str) -> bool:
        if opener is None:
            return open_external_url(uri)
        return open_external_url(uri, opener=opener)

    def _uri_from_nav_action(nav_action) -> str:
        request = nav_action.get_request() if nav_action is not None else None
        uri = request.get_uri() if request is not None else ""
        return uri if isinstance(uri, str) else ""

    def on_decide_policy(_view, decision, decision_type) -> bool:
        if decision_type not in (nav_type, new_win_type):
            return False
        uri = _uri_from_nav_action(decision.get_navigation_action())
        if decision_type == new_win_type:
            if should_open_externally(uri, app_origin):
                _open(uri)
            decision.ignore()
            return True
        if should_open_externally(uri, app_origin):
            _open(uri)
            decision.ignore()
            return True
        return False

    def on_create(_view, navigation_action):
        uri = _uri_from_nav_action(navigation_action)
        if should_open_externally(uri, app_origin):
            _open(uri)
        return None

    view.connect("decide-policy", on_decide_policy)
    view.connect("create", on_create)


def run(args: argparse.Namespace) -> int:
    context = WebKit2.WebContext.get_default()
    context.set_cache_model(WebKit2.CacheModel.DOCUMENT_VIEWER)  # 本地 127.0.0.1 主窗口禁磁盘启发式缓存，避免升级后读旧 CSS/JS
    context.clear_cache()
    context.allow_tls_certificate_for_host(Gio.TlsCertificate.new_from_file(args.cert), TRUSTED_HOST)
    view = WebKit2.WebView.new_with_context(context)
    wire_external_link_handlers(view, args.url, WebKit2)
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


__all__ = ["run", "wire_external_link_handlers"]
