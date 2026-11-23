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
    process_identity,
    session_record,
    spawn_child,
    windows_subprocess_kwargs,
)
from _procfs import session_members

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="真实进程用例依赖 Linux /proc")
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
