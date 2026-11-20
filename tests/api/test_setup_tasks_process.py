"""U5 安装任务进程化：真实 worker 子进程（替身动作）写任务文件、服务端只读；换新管理器实例（服务端重启）可恢复；
worker 被杀 → interrupted → 重试先清理；宿主应用退出 → worker 中止并清理；磁盘不足立即失败；同类运行中不重复。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _contracts import ErrorCode, LogicError, SetupTask, SetupTaskKind, SetupTaskState
from _v3.setup_tasks import INTERRUPTED_BY_RESTART, TASK_RETENTION, SetupTaskManager, TaskKindSpec, read_task_file, write_task_file
from _platform.child_process import process_identity
from _v3.setup_worker import APP_EXIT_MESSAGE, NO_OWNER_MESSAGE

ROOT = Path(__file__).resolve().parents[2]
PULL, INSTALL = SetupTaskKind.ollama_pull, SetupTaskKind.ollama_install


def _wait_until(predicate, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.05)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.fixture = tmp_path / "fixture"
        self.fixture.mkdir()
        self.tasks_dir = tmp_path / "data" / "setup" / "tasks"
        self.logs = tmp_path / "data" / "logs"
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "OVI_FAKE_WORKER_DIR": str(self.fixture)}

    def manager(self, *, owner_pid: int | None = None, worker_argv=None, **kw) -> SetupTaskManager:
        def cleanup() -> None:
            (self.fixture / "manager-cleaned").write_text("x", encoding="utf-8")

        kinds = {PULL: TaskKindSpec(disk=lambda: (self.tasks_dir, 0)),
                 INSTALL: TaskKindSpec(disk=lambda: (self.tasks_dir, 0), cleanup=cleanup)}
        return SetupTaskManager(kinds=kinds, tasks_dir=self.tasks_dir, log_dir=self.logs,
                                worker_argv=worker_argv or [sys.executable, "-m", "tests.api.fake_setup_worker"],
                                owner_pid=owner_pid or os.getpid(), environ=self.env, cwd=ROOT, **kw)

    def release(self, kind: SetupTaskKind, verdict: str = "ok") -> None:
        tmp = self.fixture / f".release-{kind.value}"
        tmp.write_text(verdict, encoding="utf-8")
        os.replace(tmp, self.fixture / f"release-{kind.value}")

    def file(self, task: SetupTask):
        return read_task_file(self.tasks_dir / f"{task.task_id}.json")


def _progressing(manager: SetupTaskManager, kind: SetupTaskKind) -> SetupTask:
    return _wait_until(lambda: (t := manager.latest(kind)) and t.progress is not None and t)


def _finished(manager: SetupTaskManager, kind: SetupTaskKind) -> SetupTask:
    return _wait_until(lambda: (t := manager.latest(kind)) and t.state != SetupTaskState.running and t)


def test_worker_writes_progress_and_result_and_same_kind_runs_once(tmp_path):
    world = World(tmp_path)
    m = world.manager()
    task = m.start(PULL)
    assert task.state == SetupTaskState.running
    seen = _progressing(m, PULL)
    assert seen.task_id == task.task_id and seen.progress.percent == 25.0 and seen.message == "fixture 进行中"
    record = world.file(task)
    assert record.pid is not None and record.pid != os.getpid() and record.owner_pid == os.getpid()
    assert m.start(PULL).task_id == task.task_id  # 运行中不重复拉起
    world.release(PULL)
    done = _finished(m, PULL)
    assert (done.task_id, done.state, done.message) == (task.task_id, SetupTaskState.succeeded, "fixture 完成")
    assert m.get(task.task_id) == done


def test_new_manager_instance_recovers_running_task_and_reads_completion(tmp_path):
    world = World(tmp_path)
    task = world.manager().start(PULL)
    restarted = world.manager()  # 服务端重启：新实例只靠任务文件
    assert _progressing(restarted, PULL).task_id == task.task_id
    assert restarted.start(PULL).task_id == task.task_id
    world.release(PULL, "fail:网络被拒绝")
    done = _finished(restarted, PULL)
    assert (done.state, done.error, done.message) == (SetupTaskState.failed, ErrorCode.dependency_failed, "网络被拒绝")


def test_killed_worker_becomes_interrupted_and_retry_cleans_first(tmp_path):
    world = World(tmp_path)
    m = world.manager()
    first = m.start(INSTALL)
    _progressing(m, INSTALL)
    os.kill(world.file(first).pid, signal.SIGKILL)
    done = _finished(m, INSTALL)
    assert (done.state, done.message) == (SetupTaskState.interrupted, INTERRUPTED_BY_RESTART)
    assert not (world.fixture / "manager-cleaned").exists()
    retry = m.start(INSTALL)
    assert retry.task_id != first.task_id and (world.fixture / "manager-cleaned").exists()
    world.release(INSTALL)
    assert _finished(m, INSTALL).state == SetupTaskState.succeeded
    assert m.get(first.task_id).state == SetupTaskState.interrupted


def test_worker_aborts_and_cleans_up_when_owner_app_exits(tmp_path):
    world = World(tmp_path)
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        m = world.manager(owner_pid=app.pid)
        task = m.start(PULL)
        _progressing(m, PULL)
    finally:
        app.kill()
        app.wait()
    done = _finished(m, PULL)
    assert (done.task_id, done.state, done.message) == (task.task_id, SetupTaskState.interrupted, APP_EXIT_MESSAGE)
    assert (world.fixture / "cleaned-ollama_pull").read_text(encoding="utf-8") == "cleaned\n"


def test_worker_refuses_to_start_when_the_host_identity_is_unknown(tmp_path):
    world = World(tmp_path)
    m = world.manager(identity=lambda pid: None if pid == os.getpid() else process_identity(pid))
    m.start(PULL)
    done = _finished(m, PULL)
    assert (done.state, done.error, done.message) == (SetupTaskState.failed, ErrorCode.dependency_failed, NO_OWNER_MESSAGE)
    assert not (world.fixture / "cleaned-ollama_pull").exists()


def test_disk_shortage_unknown_kind_dead_worker_and_retention(tmp_path):
    world = World(tmp_path)
    spawned: list = []

    class Usage:
        free = 512 * 1024**2

    m = world.manager(disk_usage=lambda path: Usage(), popen=lambda *a, **k: spawned.append(a) or pytest.fail("不得拉起"))
    failed = m.start(PULL)
    assert failed.state == SetupTaskState.failed and failed.message.startswith("磁盘空间不足：需要 1.0 GB，可用 0.5 GB")
    assert m.latest(PULL) == failed and not spawned
    with pytest.raises(LogicError) as missing:
        m.start(SetupTaskKind.asr_download)
    assert missing.value.code == ErrorCode.dependency_failed
    with pytest.raises(LogicError) as unknown:
        m.get("nope")
    assert unknown.value.code == ErrorCode.not_found

    dead = world.manager(worker_argv=[sys.executable, "-c", "raise SystemExit(7)"])
    started = dead.start(INSTALL)
    crashed = _finished(dead, INSTALL)
    assert crashed.task_id == started.task_id and crashed.state == SetupTaskState.failed
    assert "安装进程未能启动（退出码 7）" in crashed.message

    for i in range(TASK_RETENTION + 3):
        old = SetupTask(task_id=f"old{i:02d}", kind=PULL, state=SetupTaskState.failed, started_at=f"2000-01-01T00:00:{i:02d}+00:00")
        write_task_file(world.tasks_dir / f"old{i:02d}.json", old, pid=None, identity=None, owner_pid=None)
    m.latest(PULL)
    assert len(list(world.tasks_dir.glob("*.json"))) == TASK_RETENTION
    assert not (world.tasks_dir / "old00.json").exists()
