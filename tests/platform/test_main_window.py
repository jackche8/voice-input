"""U8 主窗口：证书信任规则（只限 127.0.0.1 且证书逐字节相同）、stdin 命令、窗口进程入口（真实运行：缺 WebKit 退出码 3，
替身后端读到 EOF 退出 0）、应用侧启动器（在则写 show、不在则拉起、非 0 退出报底栏、关闭先 quit 后结束会话）。"""
from __future__ import annotations

import importlib.util
import io
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _desktop.main_window import MainWindow, window_command
from _paths import APP_DISPLAY_NAME, APP_USER_MODEL_ID, INTERNAL_WINDOW_FLAG, AppPaths
from _window import BackendMissing, pywebview_host, tls_policy
from _window.__main__ import EXIT_BACKEND_MISSING, choose_backend, main
from _window.commands import Quit, Show, encode, parse_command, pump_commands

ROOT = Path(__file__).resolve().parents[2]


def test_tls_trusts_only_the_app_certificate_on_loopback(tmp_path):
    original = b"\x30\x82\x01\x2c" + bytes(range(256)) + b"cert-body"  # 只验证 PEM→DER 往返与逐字节比对，不需要真证书
    cert = tmp_path / "cert.pem"
    cert.write_text(ssl.DER_cert_to_PEM_cert(original), encoding="ascii")
    der = tls_policy.load_cert_der(cert)
    assert der == original
    assert tls_policy.allow("127.0.0.1", der, der)
    assert not tls_policy.allow("localhost", der, der) and not tls_policy.allow("192.168.1.2", der, der)
    assert not tls_policy.allow("127.0.0.1", der[:-1] + b"\x00", der) and not tls_policy.allow("127.0.0.1", b"", b"")
    with pytest.raises(OSError):
        tls_policy.load_cert_der(tmp_path / "missing.pem")


def test_stdin_commands_show_quit_and_eof():
    assert parse_command('{"cmd": "show", "route": "/setup"}') == Show("/setup")
    assert parse_command('{"cmd": "quit"}') == Quit()
    for junk in ("", "not json", '{"cmd": "show", "route": "https://evil"}', '{"cmd": "show", "route": "//evil"}', "[1]",
                 '{"cmd": "reboot"}'):
        assert parse_command(junk) is None
    assert parse_command(encode(Show("/history?x=1")).decode()) == Show("/history?x=1")
    seen: list = []
    pump_commands(io.StringIO('{"cmd": "show", "route": "/a"}\njunk\n{"cmd": "show", "route": "/b"}\n'),
                  seen.append, lambda: seen.append("quit"))
    assert seen == ["/a", "/b", "quit"]  # 读到 EOF 也退出
    seen.clear()
    pump_commands(io.StringIO('{"cmd": "quit"}\n{"cmd": "show", "route": "/late"}\n'), seen.append, lambda: seen.append("quit"))
    assert seen == ["quit"]


def test_window_entry_runs_for_real_and_rejects_non_loopback(tmp_path):
    assert choose_backend("auto", "linux") == "webkit_gtk" and choose_backend("auto", "win32") == "pywebview"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    argv = [sys.executable, "-m", "_window", "--url", "https://127.0.0.1:17945/", "--cert", str(tmp_path / "c.pem")]
    bad = subprocess.run([*argv[:4], "https://example.com/", *argv[5:]], env=env, capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "只允许 https://127.0.0.1" in bad.stderr
    if importlib.util.find_spec("gi") is not None:
        pytest.skip("本解释器装有 gi：缺 WebKit 的退出码路径无法在此真实触发")
    missing = subprocess.run([*argv, "--backend", "webkit_gtk"], env=env, capture_output=True, text=True, timeout=30)
    assert missing.returncode == EXIT_BACKEND_MISSING and "WebKit2GTK 4.1" in missing.stderr

    seen: list = []

    def fake_backend(args) -> int:
        pump_commands(io.StringIO('{"cmd": "show", "route": "/setup"}\n'), lambda r: seen.append((args.url, r)),
                      lambda: seen.append("quit"))
        return 0

    assert main(argv[3:], backends={"webkit_gtk": fake_backend, "pywebview": fake_backend}) == 0
    assert seen == [("https://127.0.0.1:17945/", "/setup"), "quit"]


class FakeHandle:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.code: int | None = None
        self.stdin = io.BytesIO()
        self.terminated = False

    def poll(self):
        return self.code

    def terminate_session(self, timeout_s):
        self.terminated = True
        self.code = -15
        return [self.pid]


def _wait_until(predicate, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.02)


def test_launcher_reuses_running_window_respawns_dead_one_and_reports_failure(tmp_path):
    paths = AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r")
    spawned: list = []
    failures: list[str] = []

    def spawn(argv, *, cwd, env, log_path, stdin_pipe):
        assert stdin_pipe and cwd == paths.program_root and log_path == paths.logs / "window.log"
        spawned.append((argv, FakeHandle(100 + len(spawned))))
        return spawned[-1][1]

    window = MainWindow(paths=paths, origin="https://127.0.0.1:18945", environ={}, on_failed=failures.append, spawn=spawn,
                        close_wait_s=0.2)
    window.show("/setup")
    assert spawned[0][0] == window_command(paths, "https://127.0.0.1:18945", "/setup")
    assert "https://127.0.0.1:18945/setup" in spawned[0][0]
    window.show("/history")
    assert len(spawned) == 1 and spawned[0][1].stdin.getvalue() == encode(Show("/history"))

    paths.logs.mkdir(parents=True)
    (paths.logs / "window.log").write_text("主界面无法打开：需要 gi 的 WebKit2GTK 4.1\n", encoding="utf-8")
    spawned[0][1].code = 3
    _wait_until(lambda: failures)
    assert failures == ["主界面无法打开：主界面无法打开：需要 gi 的 WebKit2GTK 4.1"]
    window.show("/")
    assert len(spawned) == 2

    window.close()  # 不肯退出的窗口：先写 quit，等待后结束会话
    second = spawned[1][1]
    assert second.stdin.getvalue() == encode(Quit()) and second.terminated
    assert len(failures) == 1  # 关闭引起的退出不报失败


def test_window_command_uses_internal_window_flag_when_frozen(tmp_path):
    paths = AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r")
    assert window_command(paths, "https://127.0.0.1:17945", "/setup", frozen=False, executable="/py") == [
        "/py", "-m", "_window", "--url", "https://127.0.0.1:17945/setup", "--cert", str(paths.cert),
        "--title", APP_DISPLAY_NAME,
    ]
    assert window_command(paths, "https://127.0.0.1:17945", "/setup", frozen=True,
                          executable="/app/ollama-voice-input") == [
        "/app/ollama-voice-input", INTERNAL_WINDOW_FLAG, "--url", "https://127.0.0.1:17945/setup",
        "--cert", str(paths.cert), "--title", APP_DISPLAY_NAME,
    ]


# ---------------------------------------------------------------- pywebview_host (Windows WebView2 & macOS)
class _FakeEventSlot:
    def __init__(self, on_add=None) -> None:
        self.handlers: list = []
        self._on_add = on_add

    def __iadd__(self, handler):
        self.handlers.append(handler)
        if self._on_add is not None:
            self._on_add(handler)
        return self

    def fire(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class _FakeCertAction:
    AlwaysAllow = "ALWAYS_ALLOW"
    Cancel = "CANCEL"


class _FakeCoreSettings:
    def __init__(self, timeline: list[tuple]) -> None:
        self._timeline = timeline
        self._ctx_menu = False
        self.AreDevToolsEnabled = False

    @property
    def AreDefaultContextMenusEnabled(self) -> bool:
        return self._ctx_menu

    @AreDefaultContextMenusEnabled.setter
    def AreDefaultContextMenusEnabled(self, value: bool) -> None:
        self._ctx_menu = bool(value)
        self._timeline.append(("set_context_menu", self._ctx_menu))


class _FakeCoreWebView2:
    def __init__(self, timeline: list[tuple]) -> None:
        self.timeline = timeline
        self.Settings = _FakeCoreSettings(timeline)
        self.ServerCertificateErrorDetected = _FakeEventSlot(
            on_add=lambda _h: timeline.append(("bind_cert_handler",))
        )
        self.NavigationCompleted = _FakeEventSlot(
            on_add=lambda _h: timeline.append(("bind_nav_completed",))
        )
        self.WebResourceResponseReceived = _FakeEventSlot(
            on_add=lambda _h: timeline.append(("bind_resource_response",))
        )

    def Navigate(self, url: str) -> None:
        self.timeline.append(("navigate", url))


class _FakeWebView2Control:
    def __init__(self, timeline: list[tuple], *, initial_core=None) -> None:
        self.CoreWebView2 = initial_core
        self.CoreWebView2InitializationCompleted = _FakeEventSlot(
            on_add=lambda _h: timeline.append(("bind_init_completed",))
        )


class _FakeWinForm:
    def __init__(self, wv: _FakeWebView2Control, timeline: list[tuple], *, invoke_required: bool = False) -> None:
        self.webview = wv
        self.timeline = timeline
        self.InvokeRequired = invoke_required
        self.IsDisposed = False
        self.Disposing = False
        self.Icon = None
        self.invoked_delegates: list = []

    def Invoke(self, delegate) -> None:
        self.timeline.append(("invoke", type(delegate).__name__))
        self.invoked_delegates.append(delegate)
        delegate()


class _FakeWindowEvents:
    def __init__(self) -> None:
        self.before_show = _FakeEventSlot()


class _FakePyWebViewWindow:
    def __init__(self, title: str, url: str | None, html: str | None, native, timeline: list[tuple]) -> None:
        self.title = title
        self.url = url
        self.html = html
        self.native = native
        self.timeline = timeline
        self.events = _FakeWindowEvents()
        self.destroyed = False

    def load_url(self, url: str) -> None:
        self.timeline.append(("load_url", url))

    def show(self) -> None:
        self.timeline.append(("show",))

    def destroy(self) -> None:
        self.destroyed = True
        self.timeline.append(("destroy",))


class _FakePyWebViewModule:
    def __init__(self, native_factory, timeline: list[tuple], on_start) -> None:
        self._native_factory = native_factory
        self.timeline = timeline
        self._on_start = on_start
        self.settings: dict[str, object] = {}
        self.created: list[dict] = []
        self.start_calls: list[dict] = []
        self.window: _FakePyWebViewWindow | None = None

    def create_window(
        self,
        title: str,
        url: str | None = None,
        *,
        html: str | None = None,
        width: int = 0,
        height: int = 0,
        text_select: bool = False,
    ):
        self.created.append(
            {
                "title": title,
                "url": url,
                "html": html,
                "width": width,
                "height": height,
                "text_select": text_select,
            }
        )
        self.window = _FakePyWebViewWindow(title, url, html, self._native_factory(), self.timeline)
        return self.window

    def start(self, func, **kwargs) -> None:
        self.start_calls.append(dict(kwargs))
        self._on_start(self.window, func)


def _write_fake_cert(tmp_path: Path, body: bytes = b"\x30\x82\x01\x2c" + b"A" * 64) -> tuple[Path, bytes, str]:
    pem = ssl.DER_cert_to_PEM_cert(body)
    cert_path = tmp_path / "cert.pem"
    cert_path.write_text(pem, encoding="ascii")
    return cert_path, body, pem


def test_pywebview_windows_defers_navigation_binds_cert_first_and_sets_icon_and_aumid(tmp_path, capsys):
    import argparse
    import queue
    import threading

    cert_path, der, pem = _write_fake_cert(tmp_path)
    other_der = der[:-1] + b"\xff"
    other_pem = ssl.DER_cert_to_PEM_cert(other_der)
    res_dir = tmp_path / "resources"
    res_dir.mkdir()
    ico_path = res_dir / "ollama-voice-input.ico"
    ico_path.write_bytes(b"\x00\x00\x01\x00fake-ico")

    timeline: list[tuple] = []
    aumids: list[str] = []
    icon_instances: list[str] = []
    stdin_q: queue.Queue[str] = queue.Queue()
    first_handled = threading.Event()

    class _QueueStream:
        def __init__(self) -> None:
            self._delivered = 0

        def __iter__(self):
            return self

        def __next__(self) -> str:
            if self._delivered >= 1:
                first_handled.set()
            item = stdin_q.get()
            if item == "":
                raise StopIteration
            self._delivered += 1
            return item

    class _FakeDrawingIcon:
        def __init__(self, path: str) -> None:
            self.path = path
            icon_instances.append(path)

    wv = _FakeWebView2Control(timeline, initial_core=None)
    form = _FakeWinForm(wv, timeline, invoke_required=False)
    core = _FakeCoreWebView2(timeline)

    def on_start(win: _FakePyWebViewWindow, started_cb) -> None:
        win.events.before_show.fire(win)
        win.events.before_show.fire(win)  # 幂等：重复触发不得重复绑定
        started_cb()
        # 初始化完成前收到 show("/setup")：只更新 pending_url，不得提前 load_url
        stdin_q.put('{"cmd": "show", "route": "/setup"}\n')
        assert first_handled.wait(timeout=5.0)
        assert not any(ev[0] == "load_url" for ev in timeline)
        # WebView2 完成初始化：必须先绑证错、启用右键菜单、绑导航/资源诊断，再 Navigate("/setup")
        wv.CoreWebView2 = core
        wv.CoreWebView2InitializationCompleted.fire(wv, type("E", (), {"IsSuccess": True})())
        nav_idx = timeline.index(("navigate", "https://127.0.0.1:17945/setup"))
        assert timeline.index(("bind_cert_handler",)) < nav_idx
        assert timeline.index(("set_context_menu", True)) < nav_idx
        assert timeline.index(("bind_nav_completed",)) < nav_idx
        assert timeline.index(("bind_resource_response",)) < nav_idx
        assert core.Settings.AreDefaultContextMenusEnabled is True
        assert core.Settings.AreDevToolsEnabled is False
        assert len(core.ServerCertificateErrorDetected.handlers) == 1
        assert len(core.NavigationCompleted.handlers) == 1
        assert len(core.WebResourceResponseReceived.handlers) == 1

        # 导航与子资源响应诊断：成功与非本机静默；失败写 stderr 且剥离 #fragment
        nav_h = core.NavigationCompleted.handlers[0]
        nav_h(core, type("NavOk", (), {"IsSuccess": True, "WebErrorStatus": "Unknown", "NavigationId": 1})())
        nav_h(core, type("NavFail", (), {"IsSuccess": False, "WebErrorStatus": "ConnectionAborted", "NavigationId": 2})())

        res_h = core.WebResourceResponseReceived.handlers[0]

        def _res_ev(uri: str, status: int | None, reason: str = "", *, missing_resp: bool = False):
            req = type("Req", (), {"Uri": uri})()
            resp = None if missing_resp else type("Resp", (), {"StatusCode": status, "ReasonPhrase": reason})()
            return type("ResEv", (), {"Request": req, "Response": resp})()

        res_h(core, _res_ev("https://127.0.0.1:17945/css/theme.css", 200, "OK"))
        res_h(core, _res_ev("https://127.0.0.1:17945/css/theme.css", 304, "Not Modified"))
        res_h(core, _res_ev("https://example.com/external.css", 500, "Server Error"))
        res_h(core, _res_ev("https://127.0.0.1:17945/css/theme.css#token=secret_frag", 0, ""))
        res_h(core, _res_ev("https://127.0.0.1:17945/css/studio.css?_css_retry=1", 404, "Not Found"))
        res_h(core, _res_ev("https://127.0.0.1:17945/js/home.js#frag2", None, missing_resp=True))

        # 校证：1) ToPemEncoding + set_Action 放行本应用证书 + 127.0.0.1
        actions: list[str] = []

        class _PemCert:
            def ToPemEncoding(self):
                return pem

        class _SetterEvent:
            def __init__(self, uri: str, cert) -> None:
                self.RequestUri = uri
                self.ServerCertificate = cert

            def set_Action(self, value) -> None:
                actions.append(value)

        handler = core.ServerCertificateErrorDetected.handlers[0]
        handler(core, _SetterEvent("https://127.0.0.1:17945/setup", _PemCert()))
        # 2) ToX509Certificate2 + 属性 Action 回退路径
        class _X509Cert:
            def ToX509Certificate2(self):
                return type("X", (), {"GetRawCertData": lambda self: bytearray(der)})()

        class _PropEvent:
            def __init__(self, uri: str, cert) -> None:
                self.RequestUri = uri
                self.ServerCertificate = cert
                self.Action = None

        prop_ev = _PropEvent("https://127.0.0.1:17945/", _X509Cert())
        handler(core, prop_ev)
        # 3) 错误主机 / 错误证书 / 损坏 PEM 均严格 Cancel（损坏 PEM 写 stderr）
        handler(core, _SetterEvent("https://localhost:17945/", _PemCert()))
        handler(core, _SetterEvent("https://192.168.1.9:17945/", _PemCert()))
        handler(core, _SetterEvent("https://127.0.0.1:17945/", type("Bad", (), {"ToPemEncoding": lambda self: other_pem})()))
        handler(core, _SetterEvent("https://127.0.0.1:17945/", type("Broken", (), {"ToPemEncoding": lambda self: "not-pem"})()))

        assert actions == [
            _FakeCertAction.AlwaysAllow,
            _FakeCertAction.Cancel,
            _FakeCertAction.Cancel,
            _FakeCertAction.Cancel,
            _FakeCertAction.Cancel,
        ]
        assert prop_ev.Action == _FakeCertAction.AlwaysAllow

        # 初始化完成后收到 show("/history") → load_url + show，随后 quit 结束
        stdin_q.put('{"cmd": "show", "route": "/history"}\n')
        stdin_q.put('{"cmd": "quit"}\n')
        _wait_until(lambda: win.destroyed)

    mod = _FakePyWebViewModule(lambda: form, timeline, on_start)
    args = argparse.Namespace(title=APP_DISPLAY_NAME, url="https://127.0.0.1:17945/", cert=str(cert_path))
    rc = pywebview_host.run(
        args,
        webview_mod=mod,
        platform="win32",
        resources_dir=res_dir,
        stdin=_QueueStream(),
        set_aumid=lambda aid: (aumids.append(aid) or True),
        cert_action_cls=_FakeCertAction,
        drawing_icon_cls=_FakeDrawingIcon,
    )
    assert rc == 0
    assert aumids == [APP_USER_MODEL_ID]
    assert mod.settings.get("OPEN_EXTERNAL_LINKS_IN_BROWSER") is True
    assert mod.created == [
        {"title": APP_DISPLAY_NAME, "url": None, "html": "", "width": 1180, "height": 820, "text_select": True}
    ]
    assert mod.start_calls == [{"icon": str(ico_path)}]
    assert icon_instances == [str(ico_path)] and isinstance(form.Icon, _FakeDrawingIcon)
    assert ("load_url", "https://127.0.0.1:17945/history") in timeline
    err_text = capsys.readouterr().err
    assert "主界面证书校验失败" in err_text
    assert "证书校验异常" in err_text
    assert "WebView2 导航失败：status=ConnectionAborted nav_id=2" in err_text
    assert "WebView2 本地资源加载失败：status=0 reason= uri=https://127.0.0.1:17945/css/theme.css" in err_text
    assert "WebView2 本地资源加载失败：status=404 reason=Not Found uri=https://127.0.0.1:17945/css/studio.css?_css_retry=1" in err_text
    assert "WebView2 本地资源加载失败：status=None reason= uri=https://127.0.0.1:17945/js/home.js" in err_text
    assert "secret_frag" not in err_text and "frag2" not in err_text
    assert "example.com" not in err_text and "Not Modified" not in err_text and "status=200" not in err_text


def test_pywebview_windows_invoke_marshalling_disposed_guard_and_init_failure(tmp_path, capsys):
    import argparse

    cert_path, _der, _pem = _write_fake_cert(tmp_path)
    args = argparse.Namespace(title=APP_DISPLAY_NAME, url="https://127.0.0.1:17945/", cert=str(cert_path))

    # 1. InvokeRequired=True 时必须用 System.Action 委托切回 UI 线程并直接绑定就绪 CoreWebView2
    timeline: list[tuple] = []
    core = _FakeCoreWebView2(timeline)
    wv = _FakeWebView2Control(timeline, initial_core=core)
    form = _FakeWinForm(wv, timeline, invoke_required=True)

    class _FakeDotNetAction:
        def __init__(self, fn) -> None:
            self._fn = fn

        def __call__(self) -> None:
            self._fn()

    mod1 = _FakePyWebViewModule(lambda: form, timeline, lambda _win, started: started())
    rc1 = pywebview_host.run(
        args,
        webview_mod=mod1,
        platform="win32",
        resources_dir=tmp_path,
        stdin=io.StringIO('{"cmd": "quit"}\n'),
        set_aumid=lambda _a: True,
        cert_action_cls=_FakeCertAction,
        dotnet_action_cls=_FakeDotNetAction,
    )
    assert rc1 == 0
    assert ("invoke", "_FakeDotNetAction") in timeline
    assert timeline.index(("bind_cert_handler",)) < timeline.index(("navigate", "https://127.0.0.1:17945/"))

    # 2. IsDisposed=True 时不得操作已销毁窗体或触发 Invoke
    timeline2: list[tuple] = []
    wv2 = _FakeWebView2Control(timeline2, initial_core=_FakeCoreWebView2(timeline2))
    form2 = _FakeWinForm(wv2, timeline2, invoke_required=True)
    form2.IsDisposed = True
    mod2 = _FakePyWebViewModule(lambda: form2, timeline2, lambda win, started: (win.events.before_show.fire(win), started()))
    rc2 = pywebview_host.run(
        args,
        webview_mod=mod2,
        platform="win32",
        resources_dir=tmp_path,
        stdin=io.StringIO('{"cmd": "quit"}\n'),
        set_aumid=lambda _a: True,
        cert_action_cls=_FakeCertAction,
        dotnet_action_cls=_FakeDotNetAction,
    )
    assert rc2 == 0 and form2.invoked_delegates == [] and not any(ev[0] == "navigate" for ev in timeline2)

    # 3. CoreWebView2InitializationCompleted IsSuccess=False → 写 stderr、销毁窗口并返回非零退出码
    timeline3: list[tuple] = []
    wv3 = _FakeWebView2Control(timeline3, initial_core=None)
    form3 = _FakeWinForm(wv3, timeline3, invoke_required=False)

    def fail_init(win, started):
        win.events.before_show.fire(win)
        wv3.CoreWebView2InitializationCompleted.fire(
            wv3, type("E", (), {"IsSuccess": False, "InitializationException": "HRESULT 0x80070002"})()
        )
        started()

    mod3 = _FakePyWebViewModule(lambda: form3, timeline3, fail_init)
    rc3 = pywebview_host.run(
        args,
        webview_mod=mod3,
        platform="win32",
        resources_dir=tmp_path,
        stdin=io.StringIO(""),
        set_aumid=lambda _a: True,
        cert_action_cls=_FakeCertAction,
    )
    assert rc3 != 0 and mod3.window.destroyed is True
    assert "WebView2 初始化失败" in capsys.readouterr().err


def test_pywebview_macos_untouched_and_missing_backend_and_no_ignore_ssl(tmp_path, monkeypatch):
    import argparse

    cert_path, _der, _pem = _write_fake_cert(tmp_path)
    args = argparse.Namespace(title=APP_DISPLAY_NAME, url="https://127.0.0.1:17945/", cert=str(cert_path))
    aumids: list[str] = []
    timeline: list[tuple] = []
    mod = _FakePyWebViewModule(lambda: None, timeline, lambda _win, started: (started(), _wait_until(lambda: _win.destroyed)))
    rc = pywebview_host.run(
        args,
        webview_mod=mod,
        platform="darwin",
        resources_dir=tmp_path,
        stdin=io.StringIO('{"cmd": "quit"}\n'),
        set_aumid=lambda aid: (aumids.append(aid) or True),
    )
    assert rc == 0 and aumids == []
    assert mod.settings.get("OPEN_EXTERNAL_LINKS_IN_BROWSER") is True
    assert mod.created == [
        {
            "title": APP_DISPLAY_NAME,
            "url": "https://127.0.0.1:17945/",
            "html": None,
            "width": 1180,
            "height": 820,
            "text_select": True,
        }
    ]
    assert mod.start_calls == [{}]  # macOS 不传 icon

    # 缺少 pywebview 抛 BackendMissing
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(BackendMissing):
        pywebview_host.run(args, platform="darwin")

    # 严禁代码使用全局 SSL 忽略或无 await 盲清证书白名单
    src = (ROOT / "_window" / "pywebview_host.py").read_text(encoding="utf-8")
    assert "IGNORE_SSL_ERRORS" not in src
    assert "ignore-certificate-errors" not in src
    assert "ClearServerCertificateErrorActionsAsync" not in src


def test_pywebview_windows_diagnostics_and_settings_errors_are_isolated_from_cert_init(tmp_path, capsys):
    """B2 异常隔离：Settings 与诊断事件绑定/回调异常仅写 stderr 不毁窗；证书绑定异常仍致命毁窗。"""
    import argparse

    cert_path, _der, _pem = _write_fake_cert(tmp_path)
    args = argparse.Namespace(title=APP_DISPLAY_NAME, url="https://127.0.0.1:17945/", cert=str(cert_path))

    class _BrokenSettings:
        @property
        def AreDefaultContextMenusEnabled(self) -> bool:
            return False

        @AreDefaultContextMenusEnabled.setter
        def AreDefaultContextMenusEnabled(self, _value: bool) -> None:
            raise RuntimeError("settings com error")

    class _BrokenSlot:
        def __iadd__(self, _handler):
            raise RuntimeError("slot subscribe error")

    # 1. Settings 写失败 + 诊断事件绑定失败 → 仅记 stderr，仍完成 Navigate 且不毁窗
    timeline1: list[tuple] = []
    core1 = _FakeCoreWebView2(timeline1)
    core1.Settings = _BrokenSettings()  # type: ignore[assignment]
    core1.NavigationCompleted = _BrokenSlot()  # type: ignore[assignment]
    core1.WebResourceResponseReceived = _BrokenSlot()  # type: ignore[assignment]
    wv1 = _FakeWebView2Control(timeline1, initial_core=core1)
    form1 = _FakeWinForm(wv1, timeline1, invoke_required=False)
    mod1 = _FakePyWebViewModule(
        lambda: form1,
        timeline1,
        lambda win, started: (win.events.before_show.fire(win), started(), _wait_until(lambda: win.destroyed)),
    )
    rc1 = pywebview_host.run(
        args,
        webview_mod=mod1,
        platform="win32",
        resources_dir=tmp_path,
        stdin=io.StringIO('{"cmd": "quit"}\n'),
        set_aumid=lambda _a: True,
        cert_action_cls=_FakeCertAction,
    )
    err1 = capsys.readouterr().err
    assert rc1 == 0
    assert ("navigate", "https://127.0.0.1:17945/") in timeline1
    assert "启用默认右键菜单失败" in err1
    assert "绑定 NavigationCompleted 失败" in err1
    assert "绑定 WebResourceResponseReceived 失败" in err1

    # 2. 诊断回调内部遇到损坏 event 属性 → 仅写 stderr，不抛出崩溃 UI 线程
    class _ExplodingEvent:
        @property
        def IsSuccess(self):
            raise RuntimeError("nav arg broken")

        @property
        def Request(self):
            raise RuntimeError("req arg broken")

    pywebview_host._make_navigation_completed_handler()(None, _ExplodingEvent())
    pywebview_host._make_resource_response_handler()(None, _ExplodingEvent())
    err2 = capsys.readouterr().err
    assert "导航诊断回调异常" in err2 and "资源诊断回调异常" in err2

    # 3. 证书校验事件绑定失败 → 依然走致命 _fail_init 毁窗并返回非 0
    timeline3: list[tuple] = []
    core3 = _FakeCoreWebView2(timeline3)
    core3.ServerCertificateErrorDetected = _BrokenSlot()  # type: ignore[assignment]
    wv3 = _FakeWebView2Control(timeline3, initial_core=core3)
    form3 = _FakeWinForm(wv3, timeline3, invoke_required=False)
    mod3 = _FakePyWebViewModule(
        lambda: form3,
        timeline3,
        lambda win, started: (win.events.before_show.fire(win), started()),
    )
    rc3 = pywebview_host.run(
        args,
        webview_mod=mod3,
        platform="win32",
        resources_dir=tmp_path,
        stdin=io.StringIO(""),
        set_aumid=lambda _a: True,
        cert_action_cls=_FakeCertAction,
    )
    assert rc3 != 0 and mod3.window.destroyed is True
    assert not any(ev[0] == "navigate" for ev in timeline3)
    assert "证书校验初始化失败" in capsys.readouterr().err


def test_external_links_policy_and_webkit_gtk_signal_wiring(monkeypatch):
    import types

    from _window.external_links import open_external_url, should_open_externally

    origin = "https://127.0.0.1:17945/"
    assert should_open_externally("https://github.com/jackche8/voice-input", origin) is True
    assert should_open_externally("http://example.com/doc", origin) is True
    assert should_open_externally("https://127.0.0.1:18000/", origin) is True
    assert should_open_externally("https://127.0.0.1:17945/about", origin) is False
    for blocked in ("", "about:blank", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "http://bad:port"):
        assert should_open_externally(blocked, origin) is False, blocked

    opened: list[str] = []
    assert open_external_url("https://github.com/jackche8/voice-input", opener=lambda u: (opened.append(u) or True)) is True
    assert open_external_url("file:///etc/passwd", opener=lambda u: (opened.append(u) or True)) is False
    assert open_external_url("https://example.com", opener=lambda _u: (_ for _ in ()).throw(RuntimeError("no browser"))) is False
    assert opened == ["https://github.com/jackche8/voice-input"]

    # 注入假 gi / WebKit2 模块（即使在无 GTK 的隔离环境也能实测 webkit_gtk.wire_external_link_handlers）
    class _PolicyDecisionType:
        NAVIGATION_ACTION = 0
        NEW_WINDOW_ACTION = 1
        RESPONSE = 2

    fake_webkit = types.SimpleNamespace(PolicyDecisionType=_PolicyDecisionType)
    fake_repo = types.SimpleNamespace(Gio=object(), GLib=object(), Gtk=object(), WebKit2=fake_webkit)
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *_a: None  # type: ignore[attr-defined]
    fake_gi.repository = fake_repo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", fake_repo)
    monkeypatch.delitem(sys.modules, "_window.webkit_gtk", raising=False)

    import importlib

    webkit_gtk = importlib.import_module("_window.webkit_gtk")

    signals: dict[str, object] = {}

    class _FakeWebView:
        def connect(self, name: str, handler) -> None:
            signals[name] = handler

    class _FakeRequest:
        def __init__(self, uri: str) -> None:
            self._uri = uri

        def get_uri(self) -> str:
            return self._uri

    class _FakeNavAction:
        def __init__(self, uri: str) -> None:
            self._req = _FakeRequest(uri)

        def get_request(self):
            return self._req

    class _FakeDecision:
        """故意不提供已废弃的 get_request()，强制验证走 get_navigation_action().get_request().get_uri()。"""

        def __init__(self, uri: str) -> None:
            self._nav = _FakeNavAction(uri)
            self.ignored = False

        def get_navigation_action(self):
            return self._nav

        def ignore(self) -> None:
            self.ignored = True

    browser_opened: list[str] = []
    view = _FakeWebView()
    webkit_gtk.wire_external_link_handlers(
        view,
        origin,
        fake_webkit,
        opener=lambda u: (browser_opened.append(u) or True),
    )
    assert set(signals) == {"decide-policy", "create"}

    decide = signals["decide-policy"]
    create = signals["create"]

    # 1) 外部导航 → 打开系统浏览器 + ignore + 返回 True
    ext_nav = _FakeDecision("https://github.com/jackche8/voice-input/releases")
    assert decide(view, ext_nav, _PolicyDecisionType.NAVIGATION_ACTION) is True
    assert ext_nav.ignored is True

    # 2) 同源导航 → 放行（不 ignore、不开浏览器、返回 False）
    same_nav = _FakeDecision("https://127.0.0.1:17945/logs")
    assert decide(view, same_nav, _PolicyDecisionType.NAVIGATION_ACTION) is False
    assert same_nav.ignored is False

    # 3) NEW_WINDOW_ACTION：外链开浏览器并 ignore；同源也 ignore 禁弹窗
    ext_new = _FakeDecision("https://jackche8.github.io/voice-input/")
    assert decide(view, ext_new, _PolicyDecisionType.NEW_WINDOW_ACTION) is True
    assert ext_new.ignored is True

    same_new = _FakeDecision("https://127.0.0.1:17945/about")
    assert decide(view, same_new, _PolicyDecisionType.NEW_WINDOW_ACTION) is True
    assert same_new.ignored is True

    # 4) RESPONSE 策略不拦截
    resp_dec = _FakeDecision("https://github.com/x")
    assert decide(view, resp_dec, _PolicyDecisionType.RESPONSE) is False
    assert resp_dec.ignored is False

    # 5) create 信号：外链开浏览器并返回 None；同源返回 None 且不开浏览器
    assert create(view, _FakeNavAction("https://github.com/jackche8/voice-input/issues")) is None
    assert create(view, _FakeNavAction("https://127.0.0.1:17945/")) is None
    assert browser_opened == [
        "https://github.com/jackche8/voice-input/releases",
        "https://jackche8.github.io/voice-input/",
        "https://github.com/jackche8/voice-input/issues",
    ]

    # 6) webkit_gtk.run() 必须接入外链拦截信号（防止仅测辅助函数漏测入口接线）
    import argparse

    run_signals: list[str] = []

    class _RunView:
        def connect(self, name: str, _handler) -> None:
            run_signals.append(name)

        def load_uri(self, _uri: str) -> None:
            run_signals.append("load_uri")

    class _RunContext:
        def set_cache_model(self, _m) -> None: ...
        def clear_cache(self) -> None: ...
        def allow_tls_certificate_for_host(self, _c, _h) -> None: ...

    class _RunWindow:
        def __init__(self, **_kw) -> None: ...
        def set_default_size(self, _w, _h) -> None: ...
        def add(self, _v) -> None: ...
        def connect(self, _sig, _cb) -> None: ...
        def show_all(self) -> None: ...
        def destroy(self) -> None: ...

    fake_webkit.CacheModel = types.SimpleNamespace(DOCUMENT_VIEWER=0)
    fake_webkit.WebContext = types.SimpleNamespace(get_default=lambda: _RunContext())
    fake_webkit.WebView = types.SimpleNamespace(new_with_context=lambda _c: _RunView())
    fake_repo.Gio = types.SimpleNamespace(TlsCertificate=types.SimpleNamespace(new_from_file=lambda _p: object()))
    fake_repo.GLib = types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a))
    fake_repo.Gtk = types.SimpleNamespace(Window=_RunWindow, main=lambda: None, main_quit=lambda: None)
    monkeypatch.setattr(webkit_gtk, "Gio", fake_repo.Gio)
    monkeypatch.setattr(webkit_gtk, "GLib", fake_repo.GLib)
    monkeypatch.setattr(webkit_gtk, "Gtk", fake_repo.Gtk)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert webkit_gtk.run(argparse.Namespace(url=origin, cert="c.pem", title="t")) == 0
    assert "decide-policy" in run_signals and "create" in run_signals
    assert run_signals.index("decide-policy") < run_signals.index("load_uri")
