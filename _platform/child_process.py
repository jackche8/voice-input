"""组件子进程的三平台统一入口：随应用退出、整会话终止、进程身份、按记录清理残留。

- Linux：child_guard pdeathsig 包装 + 新会话；整体终止按 /proc 会话成员。
- macOS：child_guard watch 包装 + 新会话；整体终止按进程组。
- Windows：KILL_ON_JOB_CLOSE 的 Job Object；整体终止 = TerminateJobObject。
只允许在看护线程内调用 spawn_child：Linux 的 PDEATHSIG 绑定的是创建子进程的线程。
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, TextIO

from _platform.detect import detect_os
from _platform.protocol import OsName

LOG_ROTATE_BYTES = 10 * 1024 * 1024
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def _is_nt() -> bool:
    return os.name == "nt"


def windows_subprocess_kwargs(
    *,
    new_process_group: bool = False,
    encoding: str | None = None,
    errors: str | None = "replace",
) -> dict[str, object]:
    """跨平台 subprocess 关键字参数：Windows 下默认加 CREATE_NO_WINDOW，POSIX 下不下发 creationflags 与 Windows 专用的 oem 编码。"""
    kwargs: dict[str, object] = {}
    is_nt = _is_nt()
    if is_nt:
        kwargs["creationflags"] = CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if new_process_group else 0)
    if encoding is not None and (is_nt or encoding.lower() != "oem"):
        kwargs["encoding"] = encoding
    if errors is not None:
        kwargs["errors"] = errors
    return kwargs


def set_env_var(
    env: dict[str, str],
    key: str,
    value: str,
    *,
    os_name: OsName | None = None,
    overwrite: bool = True,
) -> None:
    """跨平台写入环境变量：Windows 大小写不敏感去重；overwrite=False 时视空/纯空白为未设并保留非空值（含 \"0\"/\"false\"）。"""
    target = os_name or detect_os()
    if target == "windows":
        upper = key.upper()
        existing = ""
        exact = env.get(key)
        if not overwrite and isinstance(exact, str) and exact.strip():
            existing = exact
        for cur in list(env):
            if cur.upper() != upper:
                continue
            if not overwrite and not existing and isinstance(env[cur], str) and env[cur].strip():
                existing = env[cur]
            if cur != key:
                del env[cur]
        env[key] = existing if (not overwrite and existing) else value
        return
    if not overwrite:
        cur_val = env.get(key)
        if isinstance(cur_val, str) and cur_val.strip():
            return
    env[key] = value


def unset_env_var(
    env: dict[str, str],
    key: str,
    *,
    os_name: OsName | None = None,
) -> None:
    """跨平台移除环境变量：Windows 按大小写不敏感清理全部变体，POSIX 精确移除。"""
    target = os_name or detect_os()
    if target == "windows":
        upper = key.upper()
        for cur in list(env):
            if cur.upper() == upper:
                del env[cur]
        return
    env.pop(key, None)


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


def rotate_log(log_path: Path) -> None:
    """超过 LOG_ROTATE_BYTES（10MB）时将日志改名为 `.1`（只留一份）。"""
    if log_path.is_file() and log_path.stat().st_size > LOG_ROTATE_BYTES:
        os.replace(log_path, log_path.with_name(log_path.name + ".1"))


class _TeeTextStream:
    """将主进程 stdout/stderr 同步写入原流与持久化日志文件（线程安全，支持 pythonw 原流为 None）。"""

    def __init__(self, primary: TextIO | None, sink: TextIO, lock: threading.Lock) -> None:
        self._primary = primary
        self._sink = sink
        self._lock = lock
        self.encoding = getattr(primary, "encoding", "utf-8") or "utf-8"

    def write(self, data: str) -> int:
        if not data:
            return 0
        if self._primary is not None:
            try:
                self._primary.write(data)
            except OSError:
                pass
        with self._lock:
            if not self._sink.closed:
                self._sink.write(data)
                self._sink.flush()
        return len(data)

    def flush(self) -> None:
        if self._primary is not None:
            try:
                self._primary.flush()
            except OSError:
                pass
        with self._lock:
            if not self._sink.closed:
                self._sink.flush()

    def isatty(self) -> bool:
        return bool(self._primary.isatty()) if self._primary is not None else False

    def fileno(self) -> int:
        if self._primary is not None:
            return self._primary.fileno()
        return self._sink.fileno()


class AppLogHandle:
    """主进程日志分流句柄；close() 幂等恢复原 sys.stdout/sys.stderr 并关闭日志文件。"""

    def __init__(
        self,
        log_path: Path,
        sink: TextIO,
        lock: threading.Lock,
        orig_stdout: TextIO | None,
        orig_stderr: TextIO | None,
    ) -> None:
        self.path = log_path
        self._sink = sink
        self._lock = lock
        self._orig_stdout = orig_stdout
        self._orig_stderr = orig_stderr
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if isinstance(sys.stdout, _TeeTextStream) and sys.stdout._sink is self._sink:
            sys.stdout = self._orig_stdout
        if isinstance(sys.stderr, _TeeTextStream) and sys.stderr._sink is self._sink:
            sys.stderr = self._orig_stderr
        with self._lock:
            if not self._sink.closed:
                self._sink.flush()
                self._sink.close()


def attach_app_log(log_path: Path) -> AppLogHandle:
    """按 rotate_log 轮转后以追加模式打开主进程日志，将 sys.stdout / sys.stderr 同步落盘。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(log_path)
    sink = open(log_path, "a", encoding="utf-8", buffering=1)
    lock = threading.Lock()
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeTextStream(orig_stdout, sink, lock)  # type: ignore[assignment]
    sys.stderr = _TeeTextStream(orig_stderr, sink, lock)  # type: ignore[assignment]
    return AppLogHandle(log_path, sink, lock, orig_stdout, orig_stderr)


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
    rotate_log(log_path)
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
    "AppLogHandle",
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "ChildHandle",
    "LOG_ROTATE_BYTES",
    "SessionRecord",
    "attach_app_log",
    "kill_recorded_session",
    "process_identity",
    "rotate_log",
    "session_record",
    "set_env_var",
    "spawn_child",
    "unset_env_var",
    "windows_subprocess_kwargs",
]
