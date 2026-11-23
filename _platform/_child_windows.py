"""Windows 组件子进程：创建后立即放进「句柄关闭即杀」的 Job Object；整体终止 = TerminateJobObject。

应用进程消失时内核关闭它持有的 Job 句柄，Job 内全部进程随之结束（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE）。
已知限制：Popen 创建与 AssignProcessToJobObject 之间组件若已派生孙进程，该孙进程不在 Job 内（组件启动初期不派生）。
"""
from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from _platform.child_process import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    windows_subprocess_kwargs,
)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
TERMINATED_EXIT_CODE = 1


class ProcessTreeApi(Protocol):
    def list_processes(self) -> list[tuple[int, int]]: ...
    def terminate_and_wait(self, pid: int, timeout_ms: int) -> bool: ...


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class JobApi(Protocol):
    def create_kill_on_close_job(self) -> int: ...
    def assign(self, job: int, process_handle: int) -> None: ...
    def terminate(self, job: int) -> None: ...
    def creation_time(self, pid: int) -> str | None: ...


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class CtypesJobApi:
    def __init__(self) -> None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise RuntimeError("当前系统不支持 ctypes.windll（非 Windows 平台）")
        self._k = windll.kernel32
        self._k.CreateJobObjectW.restype = ctypes.c_void_p
        self._k.OpenProcess.restype = ctypes.c_void_p

    def _check(self, ok: int, what: str) -> None:
        if not ok:
            err = ctypes.WinError()  # 读 GetLastError
            raise OSError(err.errno, f"{what} 失败：{err.strerror}")

    def create_kill_on_close_job(self) -> int:
        job = self._k.CreateJobObjectW(None, None)
        self._check(job, "CreateJobObjectW")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._check(self._k.SetInformationJobObject(
            ctypes.c_void_p(job), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info),
        ), "SetInformationJobObject")
        return int(job)

    def assign(self, job: int, process_handle: int) -> None:
        self._check(self._k.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(process_handle)),
                    "AssignProcessToJobObject")

    def terminate(self, job: int) -> None:
        self._check(self._k.TerminateJobObject(ctypes.c_void_p(job), TERMINATED_EXIT_CODE), "TerminateJobObject")

    def creation_time(self, pid: int) -> str | None:
        handle = self._k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [ctypes.c_ulonglong() for _ in range(4)]
            if not self._k.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(t) for t in times)):
                return None
            return str(times[0].value)
        finally:
            self._k.CloseHandle(ctypes.c_void_p(handle))


class WindowsChild:
    def __init__(self, proc: subprocess.Popen, *, job: int, api: JobApi) -> None:
        self._proc = proc
        self._job = job
        self._api = api
        self.pid = proc.pid
        self.stdin = proc.stdin
        self.sid = proc.pid  # Windows 无会话号；记录与清理只用 pid + 创建时间
        self.identity = api.creation_time(proc.pid)

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate_session(self, timeout_s: float) -> list[int]:
        self._api.terminate(self._job)
        try:
            self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"TerminateJobObject 后进程 {self.pid} 在 {timeout_s}s 内仍未退出") from exc
        return [self.pid]


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_file,
    api: JobApi,
    stdin_pipe: bool = False,
    popen=subprocess.Popen,
) -> WindowsChild:
    job = api.create_kill_on_close_job()
    proc = popen(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )
    try:
        api.assign(job, int(proc._handle))
    except OSError:
        proc.kill()
        raise
    return WindowsChild(proc, job=job, api=api)


def kill_recorded(pid: int, identity: str | None, *, api: JobApi, run=subprocess.run) -> list[int]:
    """应用异常退出后 Job 已随句柄关闭结束全部进程；这里只处理身份仍一致的残留（整棵树强制结束）。"""
    current = api.creation_time(pid)
    if current is None or current != identity:
        return []
    run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **windows_subprocess_kwargs(encoding="oem", errors="replace"),
    )
    return [pid]


class CtypesProcessTreeApi:
    """基于 Win32 Toolhelp 快照与 TerminateProcess/WaitForSingleObject 的后代进程树终结实现。"""

    def __init__(self) -> None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise RuntimeError("当前系统不支持 ctypes.windll（非 Windows 平台）")
        self._k = windll.kernel32
        self._k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        self._k.OpenProcess.restype = ctypes.c_void_p

    def list_processes(self) -> list[tuple[int, int]]:
        snap = self._k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            err = ctypes.WinError()
            raise OSError(err.errno, f"CreateToolhelp32Snapshot 失败：{err.strerror}")
        pairs: list[tuple[int, int]] = []
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            ok = self._k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
            while ok:
                pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                ok = self._k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
        finally:
            self._k.CloseHandle(ctypes.c_void_p(snap))
        return pairs

    def terminate_and_wait(self, pid: int, timeout_ms: int) -> bool:
        handle = self._k.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            terminated = bool(self._k.TerminateProcess(ctypes.c_void_p(handle), TERMINATED_EXIT_CODE))
            self._k.WaitForSingleObject(ctypes.c_void_p(handle), max(0, int(timeout_ms)))
            return terminated
        finally:
            self._k.CloseHandle(ctypes.c_void_p(handle))


def collect_descendant_pids(root_pid: int, pairs: Sequence[tuple[int, int]]) -> list[int]:
    """从 (pid, ppid) 列表递归收集 root_pid 的全部后代 PID（绝不包含 root_pid 自身，孙进程优先以便由叶向根清理）。"""
    children_by_parent: dict[int, list[int]] = {}
    for pid, ppid in pairs:
        if pid == root_pid or pid == ppid:
            continue
        children_by_parent.setdefault(ppid, []).append(pid)
    visited: set[int] = {root_pid}
    postorder: list[int] = []

    def dfs(parent: int) -> None:
        for child in children_by_parent.get(parent, ()):
            if child in visited:
                continue
            visited.add(child)
            dfs(child)
            postorder.append(child)

    dfs(root_pid)
    return postorder


def terminate_descendants(
    root_pid: int,
    *,
    timeout_s: float = 5.0,
    api: ProcessTreeApi | None = None,
) -> list[int]:
    """终止 root_pid 派生的全部后代进程（排除 root_pid 自身，不自杀），返回成功发送终止的 PID。"""
    tree_api = CtypesProcessTreeApi() if api is None else api
    timeout_ms = max(0, int(timeout_s * 1000))
    descendants = collect_descendant_pids(root_pid, tree_api.list_processes())
    killed: list[int] = []
    for pid in descendants:
        if pid == root_pid:
            continue
        if tree_api.terminate_and_wait(pid, timeout_ms):
            killed.append(pid)
    return killed


__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "CtypesJobApi",
    "CtypesProcessTreeApi",
    "JobApi",
    "ProcessTreeApi",
    "WindowsChild",
    "collect_descendant_pids",
    "kill_recorded",
    "spawn",
    "terminate_descendants",
]
