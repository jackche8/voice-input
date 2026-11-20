"""U9 单例唤起：真实两个进程——第二个把 show 交给已运行实例后退出 0；已运行实例无响应 → 第二个退出 1；
quit 触发退出回调；未知命令回 error；POSIX 地址是 runtime_dir 下全路径。

套接字路径受 sun_path 108 字节限制，测试临时目录太长；运行目录改用 $XDG_RUNTIME_DIR 下按进程号区分的子目录
（同产品一样不落 /tmp，用后删除），锁、数据仍在测试临时目录。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from _desktop.single_instance import send_command, socket_name
from _paths import APP_ID, default_app_paths

ROOT = Path(__file__).resolve().parents[2]
PATHS = "AppPaths(config_root=Path('c'), data_root=Path('d'), runtime_dir=Path(sys.argv[2]), os_name='linux', live=False)"
PRIMARY = f"""
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from _desktop.single_instance import SingleInstanceServer, socket_name
from _desktop.singleton import try_acquire_singleton_lock
from _paths import AppPaths
paths = {PATHS}
app = QCoreApplication([])
lock = try_acquire_singleton_lock(paths.lock)
assert lock is not None
if sys.argv[1] == "mute":  # 持锁但不监听：模拟卡死的实例
    Path("ready").write_text("x")
    import time; time.sleep(60)
server = SingleInstanceServer(socket_name(paths), on_show=lambda r: open("shown", "a").write(r + "\\n"), on_quit=app.quit)
server.listen()
Path("ready").write_text("x")
sys.exit(app.exec())
"""
SECOND = f"""
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from _desktop.single_instance import run_as_second_instance
from _desktop.singleton import try_acquire_singleton_lock
from _paths import AppPaths
paths = {PATHS}
app = QCoreApplication([])
assert try_acquire_singleton_lock(paths.lock) is None
sys.exit(run_as_second_instance(paths, "/setup", timeout_ms=int(sys.argv[1])))
"""


def _run(code: str, cwd: Path, *args: str, background: bool = False):
    argv = [sys.executable, "-c", code, *args]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "QT_QPA_PLATFORM": "offscreen"}
    if background:
        return subprocess.Popen(argv, cwd=cwd, env=env)
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=30)


@pytest.fixture
def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not Path(base).is_dir():
        pytest.skip("没有 XDG_RUNTIME_DIR：无处放短路径的本地套接字")
    path = Path(base) / f"ovi-test-{os.getpid()}"
    path.mkdir()
    yield path
    shutil.rmtree(path)


def _wait_until(predicate, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.05)


def test_second_instance_hands_show_over_and_quit_ends_the_first(tmp_path, runtime_dir):
    primary = _run(PRIMARY, tmp_path, "serve", str(runtime_dir), background=True)
    try:
        _wait_until(lambda: (tmp_path / "ready").exists())
        second = _run(SECOND, tmp_path, "3000", str(runtime_dir))
        assert second.returncode == 0, second.stdout + second.stderr
        _wait_until(lambda: (tmp_path / "shown").exists())
        assert (tmp_path / "shown").read_text() == "/setup\n"
        QCoreApplication.instance() or QCoreApplication([])
        name = str(runtime_dir / f"{APP_ID}.sock")
        raw = QLocalSocket()
        raw.connectToServer(name)
        assert raw.waitForConnected(3000)
        raw.write(b'{"cmd": "bogus"}\n')
        raw.flush()
        assert raw.waitForReadyRead(3000) and bytes(raw.readAll()) == b"error unknown-command\n"
        raw.disconnectFromServer()
        assert send_command(name, {"cmd": "quit"})
        assert primary.wait(timeout=10) == 0
    finally:
        if primary.poll() is None:
            primary.kill()
            primary.wait()


def test_unresponsive_first_instance_makes_second_exit_1(tmp_path, runtime_dir):
    mute = _run(PRIMARY, tmp_path, "mute", str(runtime_dir), background=True)
    try:
        _wait_until(lambda: (tmp_path / "ready").exists())
        second = _run(SECOND, tmp_path, "300", str(runtime_dir))
        assert second.returncode == 1 and f"已有实例未响应（PID {mute.pid}）" in second.stdout
    finally:
        mute.kill()
        mute.wait()


def test_socket_address_is_the_runtime_dir_full_path_or_a_per_user_pipe():
    linux = default_app_paths({"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/1000"}, os_name="linux")
    assert socket_name(linux) == f"/run/user/1000/{APP_ID}.sock"
    windows = default_app_paths({"APPDATA": "/w/Roaming", "LOCALAPPDATA": "/w/Local"}, os_name="windows")
    assert socket_name(windows).startswith(f"{APP_ID}-") and "/" not in socket_name(windows)
