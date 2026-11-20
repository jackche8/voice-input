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

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TERMINATED_EXIT_CODE = 1


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
    run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=30, check=False)
    return [pid]


__all__ = ["CtypesJobApi", "JobApi", "WindowsChild", "kill_recorded", "spawn"]
