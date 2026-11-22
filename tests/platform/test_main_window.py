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
from _paths import APP_DISPLAY_NAME, INTERNAL_WINDOW_FLAG, AppPaths
from _window import tls_policy
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
