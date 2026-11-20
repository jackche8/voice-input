"""POSIX 组件子进程：经 child_guard 包装以新会话启动；按会话（Linux /proc）或进程组（macOS）整体终止。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from _procfs import is_alive, proc_starttime, session_members

GUARD_SCRIPT = Path(__file__).resolve().with_name("child_guard.py")
KILL_WAIT_S = 5.0  # GPU 进程被 KILL 后驱动回收可能要几秒
POLL_S = 0.05


def guarded_argv(argv: Sequence[str], *, linux: bool, parent_pid: int) -> list[str]:
    # 按文件路径 + -I 运行：不依赖组件的 cwd/PYTHONPATH，也不导入 _platform 包（pydantic 等）
    mode = "pdeathsig" if linux else "watch"
    return [sys.executable, "-I", str(GUARD_SCRIPT), "--mode", mode, "--parent", str(parent_pid), "--", *argv]


def identity(pid: int, *, linux: bool) -> str | None:
    """Linux：/proc starttime；macOS：`ps -o lstart=`。进程不存在（或已是僵尸）→ None。"""
    if linux:
        if not is_alive(pid):
            return None
        starttime = proc_starttime(pid)
        return None if starttime is None else str(starttime)
    proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False)
    text = proc.stdout.strip()
    return text if proc.returncode == 0 and text else None


def _members(sid: int, *, linux: bool) -> list[int]:
    if linux:
        return session_members(sid)
    try:
        os.killpg(sid, 0)
    except ProcessLookupError:
        return []
    except PermissionError:
        pass
    return [sid]  # macOS 只能按进程组整体发信号，不能枚举；非空即「组里还有进程」


def _signal_members(sid: int, sig: int, *, linux: bool) -> None:
    if not linux:
        try:
            os.killpg(sid, sig)
        except ProcessLookupError:
            pass
        return
    for pid in session_members(sid):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _wait_empty(sid: int, timeout_s: float, *, linux: bool, reap: Callable[[], object]) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        reap()
        if not _members(sid, linux=linux):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def terminate_session(sid: int, timeout_s: float, *, linux: bool, reap: Callable[[], object] = lambda: None) -> list[int]:
    """TERM 会话（进程组）全部成员 → 等 timeout_s → KILL 剩余 → 再等 KILL_WAIT_S。返回收到信号的 PID（macOS 为组号）。"""
    targets = _members(sid, linux=linux)
    if not targets:
        return []
    _signal_members(sid, signal.SIGTERM, linux=linux)
    if not _wait_empty(sid, timeout_s, linux=linux, reap=reap):
        _signal_members(sid, signal.SIGKILL, linux=linux)
        if not _wait_empty(sid, KILL_WAIT_S, linux=linux, reap=reap):
            raise RuntimeError(f"会话 {sid} 在 KILL 后仍有进程：{_members(sid, linux=linux)}")
    return targets


class PosixChild:
    """一个组件进程（会话组长 = 包装进程 exec 后的组件本身，或 watch 模式下的包装进程）。"""

    def __init__(self, proc: subprocess.Popen, *, linux: bool) -> None:
        self._proc = proc
        self._linux = linux
        self.pid = proc.pid
        self.stdin = proc.stdin
        self.sid = proc.pid  # start_new_session：组长 PID 即会话号与进程组号
        self.identity = identity(proc.pid, linux=linux)

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate_session(self, timeout_s: float) -> list[int]:
        return terminate_session(self.sid, timeout_s, linux=self._linux, reap=self._proc.poll)


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_file,
    linux: bool,
    parent_pid: int,
    stdin_pipe: bool = False,
) -> PosixChild:
    proc = subprocess.Popen(
        guarded_argv(argv, linux=linux, parent_pid=parent_pid),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return PosixChild(proc, linux=linux)


__all__ = ["GUARD_SCRIPT", "PosixChild", "guarded_argv", "identity", "spawn", "terminate_session"]
