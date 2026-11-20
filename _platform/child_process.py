"""组件子进程的三平台统一入口：随应用退出、整会话终止、进程身份、按记录清理残留。

- Linux：child_guard pdeathsig 包装 + 新会话；整体终止按 /proc 会话成员。
- macOS：child_guard watch 包装 + 新会话；整体终止按进程组。
- Windows：KILL_ON_JOB_CLOSE 的 Job Object；整体终止 = TerminateJobObject。
只允许在看护线程内调用 spawn_child：Linux 的 PDEATHSIG 绑定的是创建子进程的线程。
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from _platform.detect import detect_os
from _platform.protocol import OsName

LOG_ROTATE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SessionRecord:
    """写进 run/children.json 的一条：组长 PID、会话号（macOS 为进程组号，Windows 同 PID）、组长身份。"""

    pid: int
    sid: int
    leader_starttime: str | None


class ChildHandle(Protocol):
    pid: int
    sid: int
    identity: str | None
    stdin: IO[bytes] | None  # 只有 stdin_pipe=True 拉起时才有（主窗口进程靠它收命令）

    def poll(self) -> int | None: ...

    def terminate_session(self, timeout_s: float) -> list[int]: ...


def _rotate(log_path: Path) -> None:
    if log_path.is_file() and log_path.stat().st_size > LOG_ROTATE_BYTES:
        os.replace(log_path, log_path.with_name(log_path.name + ".1"))


def spawn_child(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    stdin_pipe: bool = False,
    os_name: OsName | None = None,
) -> ChildHandle:
    """拉起组件：stdout+stderr 追加到 log_path（超过 10MB 先改名为 .1，只留一份）；stdin 默认接空设备。"""
    os_name = os_name or detect_os()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(log_path)
    with open(log_path, "ab") as log_file:  # 子进程继承后父进程即可关闭
        if os_name == "windows":
            from _platform import _child_windows

            return _child_windows.spawn(argv, cwd=cwd, env=env, log_file=log_file, stdin_pipe=stdin_pipe,
                                        api=_child_windows.CtypesJobApi())
        from _platform import _child_posix

        return _child_posix.spawn(argv, cwd=cwd, env=env, log_file=log_file, stdin_pipe=stdin_pipe,
                                  linux=os_name == "linux", parent_pid=os.getpid())


def session_record(handle: ChildHandle) -> SessionRecord:
    return SessionRecord(pid=handle.pid, sid=handle.sid, leader_starttime=handle.identity)


def process_identity(pid: int, *, os_name: OsName | None = None) -> str | None:
    """进程身份（启动时间）：Linux /proc starttime、macOS `ps -o lstart=`、Windows 创建时间；不存在 → None。"""
    os_name = os_name or detect_os()
    if os_name == "windows":
        from _platform import _child_windows

        return _child_windows.CtypesJobApi().creation_time(pid)
    from _platform import _child_posix

    return _child_posix.identity(pid, linux=os_name == "linux")


def kill_recorded_session(record: SessionRecord, *, timeout_s: float = 5.0, os_name: OsName | None = None) -> list[int]:
    """清理上次运行留下的组件会话，返回收到信号的 PID。

    组长仍在但身份与记录不同 → PID 已被复用，跳过；组长在且一致、或组长已不在 → 终止该会话全部成员
    （Linux 会话仍有成员时内核不会把该会话号分给新进程，所以按会话号找到的一定是原组件的残留）。
    """
    os_name = os_name or detect_os()
    if os_name == "windows":
        from _platform import _child_windows

        return _child_windows.kill_recorded(record.pid, record.leader_starttime, api=_child_windows.CtypesJobApi())
    from _platform import _child_posix

    linux = os_name == "linux"
    current = _child_posix.identity(record.pid, linux=linux)
    if current is not None and current != record.leader_starttime:
        return []
    return _child_posix.terminate_session(record.sid, timeout_s, linux=linux)


__all__ = [
    "ChildHandle",
    "LOG_ROTATE_BYTES",
    "SessionRecord",
    "kill_recorded_session",
    "process_identity",
    "session_record",
    "spawn_child",
]
