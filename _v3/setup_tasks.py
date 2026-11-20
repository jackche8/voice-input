"""设置页安装任务：每个任务一个独立 worker 进程，进度与结果写在任务文件里，服务端只读。

- 任务文件 `<tasks_dir>/<task_id>.json`（原子写）：SetupTask 全部字段 + pid / identity / owner_pid。
  worker 是唯一写者；服务端只在「worker 已不在」时把 running 改写为 interrupted，或为没起 worker 的任务（磁盘不足、
  worker 未能启动）写结果。
- worker 以新会话启动、不设 PDEATHSIG：服务端重启不中断安装；宿主应用（owner_pid）退出时 worker 自行中止。
- 服务端每次读取都重新扫描任务文件（服务端重启后据此恢复）；同类任务运行中再次请求返回同一任务。
- 重试（上次 failed / interrupted）先执行该类任务的清理再新建；启动前做磁盘预检；只保留最近 20 个任务文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from _atomic_file import atomic_write_bytes
from _contracts import ErrorCode, LogicError, SetupProgress, SetupTask, SetupTaskKind, SetupTaskState
from _paths import PROGRAM_ROOT
from _platform.child_process import process_identity

ProgressFn = Callable[[SetupProgress], None]
SetupAction = Callable[[ProgressFn], str]
Spawn = Callable[[Callable[[], None]], None]

TASK_RETENTION = 20
GIB = 1024**3
DISK_MARGIN_BYTES = GIB
INTERRUPTED_BY_RESTART = "安装进程已中断（服务重启或被结束），可重试"
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def thread_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="setup-task", daemon=True).start()


def _noop() -> None:
    return None


@dataclass(frozen=True)
class TaskKindSpec:
    """disk：返回（目标目录，所需字节，不含 1 GiB 余量）；cleanup：重试前删除该类任务的半成品。"""

    disk: Callable[[], tuple[Path, int]]
    cleanup: Callable[[], None] = _noop


@dataclass(frozen=True)
class TaskFile:
    task: SetupTask
    pid: int | None
    identity: str | None
    owner_pid: int | None


def write_task_file(path: Path, task: SetupTask, *, pid: int | None, identity: str | None, owner_pid: int | None) -> None:
    payload = {**task.model_dump(mode="json"), "pid": pid, "identity": identity, "owner_pid": owner_pid}
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), 0o600)


def read_task_file(path: Path) -> TaskFile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        extra = {key: raw.pop(key) for key in ("pid", "identity", "owner_pid")}
        return TaskFile(task=SetupTask.model_validate(raw), **extra)
    except (OSError, ValueError, KeyError) as exc:
        raise LogicError(ErrorCode.internal, f"安装任务文件 {path} 无法读取：{exc}") from exc


def disk_shortfall(target: Path, need_bytes: int, *, usage=shutil.disk_usage) -> str | None:
    """目标目录最近的已存在祖先所在磁盘可用空间 < 所需 + 1 GiB → 返回可见原因。"""
    probe = target
    while not probe.exists():
        probe = probe.parent
    free = usage(probe).free
    if free >= need_bytes + DISK_MARGIN_BYTES:
        return None
    return f"磁盘空间不足：需要 {(need_bytes + DISK_MARGIN_BYTES) / GIB:.1f} GB，可用 {free / GIB:.1f} GB（{probe}）"


class SetupTaskManager:
    def __init__(
        self,
        *,
        kinds: Mapping[SetupTaskKind, TaskKindSpec],
        tasks_dir: Path,
        log_dir: Path,
        worker_argv: Sequence[str],
        owner_pid: int,
        environ: Mapping[str, str],
        cwd: Path = PROGRAM_ROOT,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        identity: Callable[[int], str | None] = process_identity,
        disk_usage=shutil.disk_usage,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self._kinds = dict(kinds)
        self._dir = tasks_dir
        self._log_dir = log_dir
        self._worker_argv = list(worker_argv)
        self._owner_pid = owner_pid
        self._owner_identity = identity(owner_pid)
        self._environ = dict(environ)
        self._cwd = cwd
        self._popen = popen
        self._identity = identity
        self._disk_usage = disk_usage
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: dict[str, tuple[SetupTask, subprocess.Popen]] = {}  # 已拉起、worker 尚未写出第一份任务文件
        self._children: list[subprocess.Popen] = []

    def available(self) -> frozenset[SetupTaskKind]:
        return frozenset(self._kinds)

    def get(self, task_id: str) -> SetupTask:
        with self._lock:
            task = self._refresh().get(task_id)
        if task is None:
            raise LogicError(ErrorCode.not_found, f"未知的设置作业 {task_id}")
        return task

    def latest(self, kind: SetupTaskKind) -> SetupTask | None:
        with self._lock:
            return self._latest_unlocked(self._refresh(), kind)

    def start(self, kind: SetupTaskKind) -> SetupTask:
        spec = self._kinds.get(kind)
        if spec is None:
            raise LogicError(ErrorCode.dependency_failed, f"当前运行环境未提供动作 {kind.value}，拒绝假成功")
        with self._lock:
            current = self._latest_unlocked(self._refresh(), kind)
            if current is not None and current.state == SetupTaskState.running:
                return current
            if current is not None and current.state in (SetupTaskState.failed, SetupTaskState.interrupted):
                try:
                    spec.cleanup()
                except OSError as exc:
                    raise LogicError(ErrorCode.dependency_failed, f"清理上次未完成的 {kind.value} 失败：{exc}") from exc
            task = SetupTask(task_id=uuid.uuid4().hex, kind=kind, state=SetupTaskState.running, message="已开始",
                             started_at=self._clock())
            target, need = spec.disk()
            short = disk_shortfall(target, need, usage=self._disk_usage)
            if short is not None:
                failed = task.model_copy(update={"state": SetupTaskState.failed, "error": ErrorCode.dependency_failed,
                                                 "message": short, "finished_at": self._clock()})
                write_task_file(self._file(task.task_id), failed, pid=None, identity=None, owner_pid=self._owner_pid)
                return failed
            self._inflight[task.task_id] = (task, self._spawn_worker(task))
            return task

    # ---- 内部 --------------------------------------------------------------
    def _file(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _spawn_worker(self, task: SetupTask) -> subprocess.Popen:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        argv = [*self._worker_argv, "--task-file", str(self._file(task.task_id)), "--kind", task.kind.value,
                "--owner-pid", str(self._owner_pid), "--owner-identity", self._owner_identity or ""]
        session = {"creationflags": _CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with open(self._log_dir / f"setup-{task.kind.value}.log", "ab") as log:
            proc = self._popen(argv, cwd=str(self._cwd), env=self._environ, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, close_fds=True, **session)
        self._children.append(proc)
        return proc

    def _refresh(self) -> dict[str, SetupTask]:
        self._children = [proc for proc in self._children if proc.poll() is None]  # 回收已退出的 worker
        files = {path.stem: read_task_file(path) for path in self._dir.glob("*.json")} if self._dir.is_dir() else {}
        for task_id, (task, proc) in list(self._inflight.items()):
            if task_id in files:
                del self._inflight[task_id]
            elif proc.poll() is not None:  # worker 没写出任务文件就退出：没有并发写者，由服务端记结果
                del self._inflight[task_id]
                log = self._log_dir / f"setup-{task.kind.value}.log"
                failed = task.model_copy(update={"state": SetupTaskState.failed, "error": ErrorCode.internal,
                                                 "message": f"安装进程未能启动（退出码 {proc.returncode}），见日志 {log}",
                                                 "finished_at": self._clock()})
                write_task_file(self._file(task_id), failed, pid=proc.pid, identity=None, owner_pid=self._owner_pid)
                files[task_id] = read_task_file(self._file(task_id))
        for task_id, record in files.items():
            if record.task.state == SetupTaskState.running and not self._worker_alive(record):
                files[task_id] = self._mark_interrupted(task_id)
        self._prune(files)
        return {**{tid: rec.task for tid, rec in files.items()}, **{tid: task for tid, (task, _p) in self._inflight.items()}}

    def _worker_alive(self, record: TaskFile) -> bool:
        return record.pid is not None and record.identity is not None and self._identity(record.pid) == record.identity

    def _mark_interrupted(self, task_id: str) -> TaskFile:
        record = read_task_file(self._file(task_id))  # worker 退出前已写完终态，进程不在后再读一次才可信
        if record.task.state != SetupTaskState.running:
            return record
        task = record.task.model_copy(update={"state": SetupTaskState.interrupted, "message": INTERRUPTED_BY_RESTART,
                                              "finished_at": self._clock()})
        write_task_file(self._file(task_id), task, pid=record.pid, identity=record.identity, owner_pid=record.owner_pid)
        return TaskFile(task, record.pid, record.identity, record.owner_pid)

    def _prune(self, files: dict[str, TaskFile]) -> None:
        ordered = sorted(files, key=lambda tid: files[tid].task.started_at, reverse=True)
        for task_id in ordered[TASK_RETENTION:]:
            if files[task_id].task.state != SetupTaskState.running:
                self._file(task_id).unlink(missing_ok=True)
                del files[task_id]

    @staticmethod
    def _latest_unlocked(tasks: Mapping[str, SetupTask], kind: SetupTaskKind) -> SetupTask | None:
        same = [task for task in tasks.values() if task.kind == kind]
        return max(same, key=lambda task: task.started_at) if same else None


__all__ = [
    "INTERRUPTED_BY_RESTART",
    "ProgressFn",
    "SetupAction",
    "SetupTaskManager",
    "Spawn",
    "TaskFile",
    "TaskKindSpec",
    "disk_shortfall",
    "now_iso",
    "read_task_file",
    "thread_spawn",
    "write_task_file",
]
