"""U2 组件子进程：随应用退出、整会话清理、包装进程不在父进程消失后 exec、按记录清理（真实 Linux 进程）；Windows 用假 Job API。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _paths import INTERNAL_CHILD_GUARD_FLAG
from _platform import _child_posix, _child_windows, child_process as cp_mod
from _platform.child_process import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    LOG_ROTATE_BYTES,
    SessionRecord,
    kill_recorded_session,
    open_append_log_binary,
    open_append_log_text,
    process_identity,
    rotate_log,
    session_record,
    set_env_var,
    spawn_child,
    unset_env_var,
    windows_subprocess_kwargs,
)
from _procfs import session_members

_LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="真实进程用例依赖 Linux /proc")
ROOT = Path(__file__).resolve().parents[2]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
# 组件本体：试图自建会话（已是组长，失败即忽略，同 sidecar 的 enter_new_session），派生一个换了进程组的孙进程后自己退出
LEADER = r"""
import os, subprocess, sys
from pathlib import Path
try:
    os.setsid()
except OSError:
    pass
g = subprocess.Popen([sys.executable, "-c", "import os, time; os.setpgid(0, 0); time.sleep(60)"])
Path(sys.argv[1]).write_text(str(g.pid))
"""
# 假应用：拉起组件（组件 exec 完成后写 ready 文件），记下会话号后一直等着被测试 SIGKILL
APP = r"""
import os, sys, time
from pathlib import Path
from _platform.child_process import spawn_child
tmp = Path(sys.argv[3])
component = [sys.executable, "-c", "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(60)", str(tmp / "ready")]
h = spawn_child(component, cwd=tmp, env=os.environ, log_path=tmp / "c.log", os_name=sys.argv[1])
Path(sys.argv[2]).write_text(str(h.sid))
time.sleep(60)
"""


def _wait_until(predicate, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.05)


def _spawn_leader(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    handle = spawn_child([sys.executable, "-c", LEADER, str(pid_file)], cwd=tmp_path, env=os.environ,
                         log_path=tmp_path / "logs" / "leader.log", os_name="linux")
    _wait_until(lambda: handle.poll() is not None and pid_file.is_file() and pid_file.read_text())
    return handle, int(pid_file.read_text())


@_LINUX_ONLY
@pytest.mark.parametrize("os_name", ["linux", "macos"])  # macos = watch 模式，在 Linux 上同样可真实运行
def test_component_exits_when_app_is_killed(tmp_path, os_name):
    sid_file = tmp_path / "sid"
    app = subprocess.Popen([sys.executable, "-c", APP, os_name, str(sid_file), str(tmp_path)],
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        _wait_until(lambda: sid_file.is_file() and sid_file.read_text() and (tmp_path / "ready").is_file())
        sid = int(sid_file.read_text())
        assert len(session_members(sid)) == (1 if os_name == "linux" else 2)  # 组件已运行（watch 模式另有包装进程）
    finally:
        app.kill()
        app.wait()
    _wait_until(lambda: not session_members(sid))


@_LINUX_ONLY
@pytest.mark.parametrize("mode", ["pdeathsig", "watch"])
def test_guard_does_not_start_component_when_parent_already_gone(tmp_path, mode):
    marker = tmp_path / "started"
    argv = [sys.executable, "-I", str(_child_posix.GUARD_SCRIPT), "--mode", mode, "--parent", "1", "--",
            "touch", str(marker)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1 and "已退出" in proc.stderr
    assert not marker.exists()


def test_guarded_argv_uses_internal_flag_when_frozen():
    src = _child_posix.guarded_argv(["ollama", "serve"], linux=True, parent_pid=42, frozen=False, executable="/py")
    assert src == ["/py", "-I", str(_child_posix.GUARD_SCRIPT), "--mode", "pdeathsig", "--parent", "42", "--",
                   "ollama", "serve"]
    frz = _child_posix.guarded_argv(["ollama", "serve"], linux=False, parent_pid=42, frozen=True,
                                    executable="/app/ollama-voice-input")
    assert frz == ["/app/ollama-voice-input", INTERNAL_CHILD_GUARD_FLAG, "--mode", "watch", "--parent", "42", "--",
                   "ollama", "serve"]


@_LINUX_ONLY
def test_terminate_session_clears_leftovers_after_component_exit_and_rotates_log(tmp_path):
    log = tmp_path / "logs" / "leader.log"
    log.parent.mkdir()
    with open(log, "wb") as fh:
        fh.truncate(LOG_ROTATE_BYTES + 1)
    handle, grandchild = _spawn_leader(tmp_path)
    assert (tmp_path / "logs" / "leader.log.1").stat().st_size == LOG_ROTATE_BYTES + 1
    assert session_members(handle.sid) == [grandchild]  # 组件已退出，换了进程组的孙进程仍在同一会话
    assert handle.terminate_session(5.0) == [grandchild]
    assert process_identity(grandchild, os_name="linux") is None
    assert handle.terminate_session(5.0) == []


@_LINUX_ONLY
def test_recorded_session_cleanup_kills_members_of_dead_leader_and_skips_reused_pid(tmp_path):
    handle, grandchild = _spawn_leader(tmp_path)
    assert kill_recorded_session(session_record(handle), os_name="linux") == [grandchild]
    assert process_identity(grandchild, os_name="linux") is None

    alive = spawn_child(SLEEPER, cwd=tmp_path, env=os.environ, log_path=tmp_path / "s.log", os_name="linux")
    try:
        assert alive.identity == process_identity(alive.pid, os_name="linux") is not None
        reused = SessionRecord(pid=alive.pid, sid=alive.sid, leader_starttime="0")
        assert kill_recorded_session(reused, os_name="linux") == []
        assert alive.poll() is None
    finally:
        assert alive.terminate_session(5.0) == [alive.pid]


@_LINUX_ONLY
@pytest.mark.parametrize("os_name", ["linux", "macos"])
def test_stdin_pipe_reaches_the_component_through_the_guard(tmp_path, os_name):
    out = tmp_path / "got"
    echo = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.stdin.readline())", str(out)]
    handle = spawn_child(echo, cwd=tmp_path, env=os.environ, log_path=tmp_path / "e.log", stdin_pipe=True, os_name=os_name)
    handle.stdin.write(b'{"cmd": "show", "route": "/setup"}\n')
    handle.stdin.flush()
    _wait_until(lambda: handle.poll() is not None)
    assert out.read_text() == '{"cmd": "show", "route": "/setup"}\n'
    assert spawn_child(SLEEPER[:2] + ["pass"], cwd=tmp_path, env=os.environ, log_path=tmp_path / "n.log",
                       os_name=os_name).stdin is None


class _FakeJobApi:
    def __init__(self, created: str | None = "132000000000000000") -> None:
        self.calls: list[tuple] = []
        self.created = created

    def create_kill_on_close_job(self) -> int:
        self.calls.append(("create",))
        return 77

    def assign(self, job: int, process_handle: int) -> None:
        self.calls.append(("assign", job, process_handle))

    def terminate(self, job: int) -> None:
        self.calls.append(("terminate", job))

    def creation_time(self, pid: int) -> str | None:
        return self.created


class _FakeProc:
    pid = 4242
    _handle = 9001
    stdin = None

    def __init__(self, argv, **kwargs) -> None:
        self.argv, self.kwargs = argv, kwargs

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 1


def test_windows_child_is_assigned_to_kill_on_close_job_and_terminated_with_it(tmp_path):
    api = _FakeJobApi()
    with open(tmp_path / "w.log", "ab") as log:
        child = _child_windows.spawn(["ollama.exe", "serve"], cwd=tmp_path, env={}, log_file=log, api=api, popen=_FakeProc)
    flags = child._proc.kwargs["creationflags"]
    assert flags & _child_windows.CREATE_NEW_PROCESS_GROUP and flags & _child_windows.CREATE_NO_WINDOW
    assert api.calls == [("create",), ("assign", 77, 9001)]
    assert child.terminate_session(1.0) == [4242] and api.calls[-1] == ("terminate", 77)

    ran: list[tuple[list[str], dict]] = []
    run = lambda argv, **kw: ran.append((list(argv), dict(kw)))  # noqa: E731
    assert _child_windows.kill_recorded(4242, "other", api=api, run=run) == [] and ran == []
    assert _child_windows.kill_recorded(4242, api.created, api=api, run=run) == [4242]
    assert ran[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert "encoding" not in ran[0][1] and ran[0][1]["errors"] == "replace"


def test_windows_subprocess_kwargs_splits_nt_and_posix(monkeypatch):
    monkeypatch.setattr(cp_mod.os, "name", "posix")
    assert windows_subprocess_kwargs(encoding="oem") == {"errors": "replace"}
    assert windows_subprocess_kwargs(encoding="utf-8") == {"encoding": "utf-8", "errors": "replace"}
    assert windows_subprocess_kwargs(new_process_group=True, errors=None) == {}

    monkeypatch.setattr(cp_mod.os, "name", "nt")
    assert windows_subprocess_kwargs(encoding="oem") == {
        "creationflags": CREATE_NO_WINDOW,
        "encoding": "oem",
        "errors": "replace",
    }
    assert windows_subprocess_kwargs(encoding="utf-8", errors="replace") == {
        "creationflags": CREATE_NO_WINDOW,
        "encoding": "utf-8",
        "errors": "replace",
    }
    assert windows_subprocess_kwargs(new_process_group=True, errors=None) == {
        "creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    }

    api = _FakeJobApi()
    seen: list[dict] = []
    assert _child_windows.kill_recorded(4242, api.created, api=api, run=lambda argv, **kw: seen.append(dict(kw))) == [4242]
    assert seen[0]["creationflags"] == CREATE_NO_WINDOW
    assert seen[0]["encoding"] == "oem" and seen[0]["errors"] == "replace"


def test_windows_terminate_descendants_kills_tree_without_killing_root():
    class _FakeTreeApi:
        def __init__(self, pairs: list[tuple[int, int]]) -> None:
            self._pairs = pairs
            self.killed: list[tuple[int, int]] = []

        def list_processes(self) -> list[tuple[int, int]]:
            return list(self._pairs)

        def terminate_and_wait(self, pid: int, timeout_ms: int) -> bool:
            self.killed.append((pid, timeout_ms))
            return True

    # 进程树：root=100 -> 200 -> 300；root=100 -> 201；无关进程 999；含自环 100->100 与 400<->401
    fake_api = _FakeTreeApi([(100, 100), (200, 100), (300, 200), (201, 100), (999, 1), (400, 401), (401, 400)])
    killed = _child_windows.terminate_descendants(100, timeout_s=2.5, api=fake_api)
    assert set(killed) == {200, 300, 201}
    assert 100 not in killed and 999 not in killed
    # 孙进程 300 先于父子进程 200 被终结
    assert killed.index(300) < killed.index(200)
    assert all(timeout_ms == 2500 for _pid, timeout_ms in fake_api.killed)


def test_set_and_unset_env_var_cross_platform():
    # 1. Windows overwrite=True：清理全部大小写变体并写入唯一规范键
    win_env = {"uv_no_config": "0", "Uv_No_Config": "x", "PATH": "C:\\Windows"}
    set_env_var(win_env, "UV_NO_CONFIG", "1", os_name="windows", overwrite=True)
    assert win_env == {"PATH": "C:\\Windows", "UV_NO_CONFIG": "1"}

    # 2. Windows overwrite=False：保留非空 "0"/"false"（优先精确键并归一到规范键），空/纯空白视为未设
    win_defaults = {
        "uv_system_certs": "0",
        "Uv_Http_Timeout": "false",
        "uv_concurrent_downloads": "   ",
        "UV_EXACT": "keep-exact",
        "uv_exact": "lower-ignored",
    }
    set_env_var(win_defaults, "UV_SYSTEM_CERTS", "1", os_name="windows", overwrite=False)
    set_env_var(win_defaults, "UV_HTTP_TIMEOUT", "120", os_name="windows", overwrite=False)
    set_env_var(win_defaults, "UV_CONCURRENT_DOWNLOADS", "8", os_name="windows", overwrite=False)
    set_env_var(win_defaults, "UV_EXACT", "fallback", os_name="windows", overwrite=False)
    assert win_defaults == {
        "UV_SYSTEM_CERTS": "0",
        "UV_HTTP_TIMEOUT": "false",
        "UV_CONCURRENT_DOWNLOADS": "8",
        "UV_EXACT": "keep-exact",
    }

    # 3. Windows unset_env_var：大小写不敏感移除全部变体
    win_color = {"FORCE_COLOR": "1", "force_color": "always", "ClColor": "stay"}
    unset_env_var(win_color, "FORCE_COLOR", os_name="windows")
    assert win_color == {"ClColor": "stay"}

    # 4. POSIX 区分大小写：小写同名键不影响大写键；空/纯空白视为未设；非空 "0" 保留
    posix_env = {"uv_system_certs": "0", "UV_HTTP_TIMEOUT": "  ", "UV_CONCURRENT_DOWNLOADS": "0", "force_color": "1", "FORCE_COLOR": "1"}
    set_env_var(posix_env, "UV_SYSTEM_CERTS", "1", os_name="linux", overwrite=False)
    set_env_var(posix_env, "UV_HTTP_TIMEOUT", "120", os_name="linux", overwrite=False)
    set_env_var(posix_env, "UV_CONCURRENT_DOWNLOADS", "8", os_name="linux", overwrite=False)
    unset_env_var(posix_env, "FORCE_COLOR", os_name="linux")
    assert posix_env == {
        "uv_system_certs": "0",
        "UV_SYSTEM_CERTS": "1",
        "UV_HTTP_TIMEOUT": "120",
        "UV_CONCURRENT_DOWNLOADS": "0",
        "force_color": "1",
    }


def test_windows_open_append_log_flags_and_two_stage_cleanup(tmp_path, monkeypatch):
    import ctypes
    import io

    # 1) 标志校验：FILE_APPEND_DATA=0x0004 且绝对不含 FILE_WRITE_DATA=0x0002
    assert _child_windows.FILE_APPEND_DATA == 0x0004
    assert (_child_windows.FILE_APPEND_DATA & 0x0002) == 0

    # 2) CtypesWinFileApi.create_append_file 实传参数校验（用假 kernel32）
    create_calls: list[tuple] = []

    class _FakeFn:
        def __init__(self, impl) -> None:
            self._impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._impl(*args)

    class _FakeKernel32:
        def __init__(self, handle_ret: int) -> None:
            self.CreateFileW = _FakeFn(lambda *a: (create_calls.append(a) or handle_ret))
            self.CloseHandle = _FakeFn(lambda _h: 1)

    fake_windll = type("W", (), {"kernel32": _FakeKernel32(1234)})()
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    win_api = _child_windows.CtypesWinFileApi()
    assert win_api.create_append_file("C:\\logs\\app.log") == 1234
    assert create_calls[0][1] == (_child_windows.FILE_APPEND_DATA | _child_windows.SYNCHRONIZE)
    assert create_calls[0][2] == (
        _child_windows.FILE_SHARE_READ | _child_windows.FILE_SHARE_WRITE | _child_windows.FILE_SHARE_DELETE
    )
    assert create_calls[0][4] == _child_windows.OPEN_ALWAYS
    assert create_calls[0][5] == _child_windows.FILE_ATTRIBUTE_NORMAL

    # 3) 两阶段无泄露清理：open_osfhandle 失败仅关 handle；fdopen 失败仅关 fd 绝不双关 handle
    class _FakeWinFileApi:
        def __init__(self, *, fail_osf: bool = False, fail_fdopen: bool = False) -> None:
            self.fail_osf = fail_osf
            self.fail_fdopen = fail_fdopen
            self.events: list[tuple] = []

        def create_append_file(self, path: str) -> int:
            self.events.append(("create", path))
            return 555

        def close_handle(self, handle: int) -> None:
            self.events.append(("close_handle", handle))

        def open_osfhandle(self, handle: int, flags: int) -> int:
            self.events.append(("open_osfhandle", handle, flags))
            if self.fail_osf:
                raise OSError("osf failed")
            return 88

        def fdopen(self, fd: int, mode: str, **kwargs):
            self.events.append(("fdopen", fd, mode, dict(kwargs)))
            if self.fail_fdopen:
                raise OSError("fdopen failed")
            return io.BytesIO() if "b" in mode else io.StringIO()

        def close_fd(self, fd: int) -> None:
            self.events.append(("close_fd", fd))

    log_path = tmp_path / "win.log"
    expected_flags = os.O_APPEND | getattr(os, "O_BINARY", 0)

    ok_bin = _FakeWinFileApi()
    _child_windows.open_append_log_binary(log_path, api=ok_bin)
    assert ok_bin.events == [
        ("create", os.fspath(log_path)),
        ("open_osfhandle", 555, expected_flags),
        ("fdopen", 88, "ab", {}),
    ]

    ok_txt = _FakeWinFileApi()
    _child_windows.open_append_log_text(log_path, api=ok_txt)
    assert ok_txt.events[-1] == ("fdopen", 88, "a", {"encoding": "utf-8", "buffering": 1})

    fail1 = _FakeWinFileApi(fail_osf=True)
    with pytest.raises(OSError, match="osf failed"):
        _child_windows.open_append_log_binary(log_path, api=fail1)
    assert ("close_handle", 555) in fail1.events
    assert not any(ev[0] == "close_fd" for ev in fail1.events)

    fail2 = _FakeWinFileApi(fail_fdopen=True)
    with pytest.raises(OSError, match="fdopen failed"):
        _child_windows.open_append_log_text(log_path, api=fail2)
    assert ("close_fd", 88) in fail2.events
    assert not any(ev[0] == "close_handle" for ev in fail2.events)


def test_open_append_log_dispatches_on_sys_platform_not_os_name(tmp_path, monkeypatch):
    import io

    # 即使 os.name/_is_nt 被 monkeypatch 成 nt/True，Linux 下仍不走 _child_windows（避免触发无 msvcrt 崩溃）
    monkeypatch.setattr(cp_mod.os, "name", "nt")
    monkeypatch.setattr(cp_mod, "_is_nt", lambda: True)
    monkeypatch.setattr(cp_mod.sys, "platform", "linux")
    called: list[str] = []
    monkeypatch.setattr(
        _child_windows,
        "open_append_log_binary",
        lambda p: (called.append(f"bin:{p}") or io.BytesIO()),
    )
    monkeypatch.setattr(
        _child_windows,
        "open_append_log_text",
        lambda p, **_kw: (called.append(f"txt:{p}") or io.StringIO()),
    )

    p = tmp_path / "logs" / "a.log"
    with open_append_log_binary(p) as fh:
        fh.write(b"hello\n")
    with open_append_log_text(p) as fh:
        fh.write("world\n")
    assert called == []
    assert p.read_text(encoding="utf-8") == "hello\nworld\n"

    # 真实 sys.platform == "win32" 时才分派到 _child_windows
    monkeypatch.setattr(cp_mod.sys, "platform", "win32")
    open_append_log_binary(p)
    open_append_log_text(p)
    assert called == [f"bin:{p}", f"txt:{p}"]


def test_rotate_log_trims_max_lines_skips_within_limit_and_refuses_symlink(tmp_path):
    with pytest.raises(ValueError, match="max_lines"):
        rotate_log(tmp_path / "x.log", max_lines=0)

    # 符号链接绝不跟随，既不按 >10MB 改名 .1 也不按 max_lines 改写目标文件
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"l1\nl2\nl3\nl4\n")
    sym = tmp_path / "sym.log"
    sym.symlink_to(outside)
    rotate_log(sym, max_lines=2)
    assert outside.read_bytes() == b"l1\nl2\nl3\nl4\n" and sym.is_symlink()

    outside_big = tmp_path / "outside_big.log"
    with open(outside_big, "wb") as fh:
        fh.truncate(LOG_ROTATE_BYTES + 1)
    sym_big = tmp_path / "sym_big.log"
    sym_big.symlink_to(outside_big)
    rotate_log(sym_big, max_lines=2)
    assert sym_big.is_symlink() and not sym_big.with_name("sym_big.log.1").exists()

    # 行数未超限（含行内 \r 进度条不计换行）：绝不重写文件（保留 inode 与 mtime_ns）
    within = tmp_path / "within.log"
    within.write_bytes(b"p1\rp2\rp3\nline2\n")
    st_before = within.stat()
    rotate_log(within, max_lines=2)
    st_after = within.stat()
    assert st_after.st_ino == st_before.st_ino and st_after.st_mtime_ns == st_before.st_mtime_ns
    assert within.read_bytes() == b"p1\rp2\rp3\nline2\n"

    # 超限时按严格 \n 裁剪保留精确尾行（保留 CRLF 与无尾换行）
    crlf = tmp_path / "crlf.log"
    crlf.write_bytes(b"a\r\nb\r\nc\r\nd\r\n")
    rotate_log(crlf, max_lines=2)
    assert crlf.read_bytes() == b"c\r\nd\r\n"

    no_nl = tmp_path / "no_nl.log"
    no_nl.write_bytes(b"1\n2\n3\n4")
    rotate_log(no_nl, max_lines=2)
    assert no_nl.read_bytes() == b"3\n4"

    # >10MB 轮转优先于按行裁剪
    huge = tmp_path / "huge.log"
    with open(huge, "wb") as fh:
        fh.truncate(LOG_ROTATE_BYTES + 1)
    rotate_log(huge, max_lines=5)
    assert not huge.exists()
    assert huge.with_name("huge.log.1").stat().st_size == LOG_ROTATE_BYTES + 1
