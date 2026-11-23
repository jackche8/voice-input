"""Windows / macOS 主窗口：pywebview（WebView2 / WKWebView）。

信任策略：
- Windows：先以 `html=""` 建窗，在 UI 线程注册 `CoreWebView2InitializationCompleted`，先绑定
  `ServerCertificateErrorDetected`（仅按 `tls_policy.allow` 放行 127.0.0.1 且证书 DER 逐字节相同），
  再在原子锁内导航到目标地址；任何解析异常均强制 `Cancel` 并写 `sys.stderr`。
- macOS：依赖本应用 CA 已加入登录钥匙串（`_platform.certs`），保持直接加载 `args.url`，不放行任何证书错误。
- 三端均禁止任何全局证书忽略开关。
"""
from __future__ import annotations

import argparse
import ssl
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from _paths import APP_USER_MODEL_ID, RESOURCES_DIR
from _platform._win32_api import set_windows_app_user_model_id
from _window import BackendMissing
from _window.commands import pump_commands
from _window.tls_policy import allow, load_cert_der

_WINDOWS_ICON_FILENAME = "ollama-voice-input.ico"


def _import_webview():
    try:
        import webview
    except ImportError as exc:
        raise BackendMissing(f"需要 pywebview：{exc}") from exc
    return webview


def _with_route(url: str, route: str) -> str:
    parts = urlsplit(url)
    path, _, query = route.partition("?")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _is_disposed(obj) -> bool:
    return bool(getattr(obj, "IsDisposed", False) or getattr(obj, "Disposing", False))


def _resolve_cert_action(action_cls=None):
    if action_cls is not None:
        return action_cls
    from Microsoft.Web.WebView2.Core import CoreWebView2ServerCertificateErrorAction  # type: ignore[import-not-found]

    return CoreWebView2ServerCertificateErrorAction


def _resolve_dotnet_action(action_cls=None):
    if action_cls is not None:
        return action_cls
    try:
        from System import Action  # type: ignore[import-not-found]

        return Action
    except ImportError:
        return None


def _resolve_drawing_icon(icon_cls=None):
    if icon_cls is not None:
        return icon_cls
    try:
        from System.Drawing import Icon  # type: ignore[import-not-found]

        return Icon
    except ImportError:
        return None


def _invoke_on_ui(control, fn: Callable[[], None], dotnet_action_cls=None) -> bool:
    if control is None or _is_disposed(control):
        return False

    def _safe_run() -> None:
        if _is_disposed(control):
            return
        fn()

    if getattr(control, "InvokeRequired", False):
        action_factory = _resolve_dotnet_action(dotnet_action_cls)
        delegate = action_factory(_safe_run) if action_factory is not None else _safe_run
        control.Invoke(delegate)
        return True
    _safe_run()
    return True


def _resolve_webview2_control(native_form):
    for candidate in (
        getattr(native_form, "webview", None),
        getattr(getattr(native_form, "browser", None), "webview", None),
    ):
        if candidate is None:
            continue
        if hasattr(candidate, "CoreWebView2") or hasattr(candidate, "CoreWebView2InitializationCompleted"):
            return candidate
        nested = getattr(candidate, "webview", None)
        if nested is not None and (
            hasattr(nested, "CoreWebView2") or hasattr(nested, "CoreWebView2InitializationCompleted")
        ):
            return nested
    return getattr(native_form, "webview", None)


def _extract_cert_der(server_cert) -> bytes:
    to_pem = getattr(server_cert, "ToPemEncoding", None)
    if callable(to_pem):
        return ssl.PEM_cert_to_DER_cert(str(to_pem()))
    to_x509 = getattr(server_cert, "ToX509Certificate2", None)
    if callable(to_x509):
        return bytes(to_x509().GetRawCertData())
    raise RuntimeError("CoreWebView2Certificate 缺少 ToPemEncoding / ToX509Certificate2 方法")


def _set_cert_action(event, action_value) -> None:
    setter = getattr(event, "set_Action", None)
    if callable(setter):
        setter(action_value)
        return
    event.Action = action_value


def _make_cert_error_handler(action_cls, app_cert_der: bytes):
    """WebView2：证书错误时比对出示证书（DER）与本应用证书，相同且主机为 127.0.0.1 才放行，否则取消。"""

    def on_error(_sender, event) -> None:
        try:
            uri = str(event.RequestUri)
            host = urlsplit(uri).hostname or ""
            presented = _extract_cert_der(event.ServerCertificate)
            if allow(host, presented, app_cert_der):
                _set_cert_action(event, action_cls.AlwaysAllow)
            else:
                print(f"主界面证书校验失败：{uri}（已拒绝）", file=sys.stderr, flush=True)
                _set_cert_action(event, action_cls.Cancel)
        except Exception as exc:
            print(f"[window] WebView2 证书校验异常，已拒绝连接：{exc}", file=sys.stderr, flush=True)
            try:
                _set_cert_action(event, action_cls.Cancel)
            except Exception as cancel_exc:
                print(f"[window] WebView2 设置拒绝动作失败：{cancel_exc}", file=sys.stderr, flush=True)

    return on_error


def _apply_form_icon(native_form, ico_str: str | None, drawing_icon_cls=None) -> None:
    if not ico_str or native_form is None or _is_disposed(native_form):
        return
    icon_cls = _resolve_drawing_icon(drawing_icon_cls)
    if icon_cls is None:
        return
    try:
        native_form.Icon = icon_cls(ico_str)
    except Exception as exc:
        print(f"[window] 设置 WinForms 窗口图标失败：{exc}", file=sys.stderr, flush=True)


def run(
    args: argparse.Namespace,
    *,
    webview_mod=None,
    platform: str | None = None,
    resources_dir: Path = RESOURCES_DIR,
    stdin=None,
    set_aumid: Callable[[str], bool] = set_windows_app_user_model_id,
    cert_action_cls=None,
    dotnet_action_cls=None,
    drawing_icon_cls=None,
) -> int:
    webview = _import_webview() if webview_mod is None else webview_mod
    settings = getattr(webview, "settings", None)
    if isinstance(settings, dict):
        settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    app_cert_der = load_cert_der(args.cert)
    in_stream = sys.stdin if stdin is None else stdin
    resolved_platform = sys.platform if platform is None else platform

    if resolved_platform != "win32":
        window = webview.create_window(args.title, args.url, width=1180, height=820)

        def started_non_win() -> None:
            threading.Thread(
                target=lambda: pump_commands(
                    in_stream,
                    lambda r: (window.load_url(_with_route(args.url, r)), window.show()),
                    window.destroy,
                ),
                name="window-stdin",
                daemon=True,
            ).start()

        webview.start(started_non_win)
        return 0

    set_aumid(APP_USER_MODEL_ID)
    ico_path = Path(resources_dir) / _WINDOWS_ICON_FILENAME
    icon_arg = str(ico_path) if ico_path.is_file() else None
    window = webview.create_window(args.title, html="", width=1180, height=820)

    state_lock = threading.Lock()
    pending_url = args.url
    ready = False
    attached = False
    cert_bound = False
    init_failed = False
    exit_code = 0

    def _fail_init(message: str) -> None:
        nonlocal init_failed, exit_code
        with state_lock:
            if init_failed:
                return
            init_failed = True
            exit_code = 1
        print(f"[window] {message}", file=sys.stderr, flush=True)
        try:
            window.destroy()
        except Exception:
            pass

    def _bind_core_and_navigate(core, native_form) -> None:
        nonlocal cert_bound, ready
        if _is_disposed(native_form):
            return
        try:
            action_cls = _resolve_cert_action(cert_action_cls)
            on_error = _make_cert_error_handler(action_cls, app_cert_der)
            with state_lock:
                if cert_bound or init_failed:
                    return
                core.ServerCertificateErrorDetected += on_error
                cert_bound = True
                ready = True
                target_url = pending_url
            core.Navigate(target_url)
        except Exception as exc:
            _fail_init(f"WebView2 证书校验初始化失败：{exc}")

    def _attach_on_ui() -> None:
        nonlocal attached
        native_form = getattr(window, "native", None)
        if native_form is None or _is_disposed(native_form):
            return
        _apply_form_icon(native_form, icon_arg, drawing_icon_cls)
        with state_lock:
            if attached or init_failed:
                return
            attached = True
        wv = _resolve_webview2_control(native_form)
        if wv is None:
            _fail_init("无法获取 WebView2 控件（window.native.webview 为空）")
            return
        core = getattr(wv, "CoreWebView2", None)
        if core is not None:
            _bind_core_and_navigate(core, native_form)
            return

        def on_init_completed(_sender, event) -> None:
            if _is_disposed(native_form):
                return
            is_success = bool(getattr(event, "IsSuccess", False))
            init_core = getattr(wv, "CoreWebView2", None)
            if not is_success or init_core is None:
                detail = getattr(event, "InitializationException", None)
                _fail_init(
                    f"WebView2 初始化失败（IsSuccess={is_success}, CoreWebView2={init_core is not None}, error={detail}）"
                )
                return
            _bind_core_and_navigate(init_core, native_form)

        wv.CoreWebView2InitializationCompleted += on_init_completed
        late_core = getattr(wv, "CoreWebView2", None)
        if late_core is not None:
            _bind_core_and_navigate(late_core, native_form)

    def _attach_windows_webview(*_event_args) -> None:
        with state_lock:
            if attached or init_failed:
                return
        native_form = getattr(window, "native", None)
        if native_form is None or _is_disposed(native_form):
            return
        _invoke_on_ui(native_form, _attach_on_ui, dotnet_action_cls)

    events = getattr(window, "events", None)
    before_show = getattr(events, "before_show", None) if events is not None else None
    if before_show is not None:
        before_show += _attach_windows_webview

    def handle_show(route: str) -> None:
        nonlocal pending_url
        next_url = _with_route(args.url, route)
        with state_lock:
            if init_failed:
                return
            pending_url = next_url
            is_ready = ready
        if is_ready:
            window.load_url(next_url)
            window.show()

    def started_win() -> None:
        _attach_windows_webview()
        threading.Thread(
            target=lambda: pump_commands(in_stream, handle_show, window.destroy),
            name="window-stdin",
            daemon=True,
        ).start()

    start_kwargs = {"icon": icon_arg} if icon_arg is not None else {}
    webview.start(started_win, **start_kwargs)
    return exit_code


__all__ = ["run"]
