"""U5b 一键装齐：五步顺序与按事实跳过；wait_ollama 读组件快照；任务失败停步；再次开始从失败步重试；
新实例读 install_all.json 恢复；运行中重复开始返回同一状态；POST /api/setup/install-all 仅本机。"""
from __future__ import annotations

import threading
import time
import uuid

from _contracts import (
    ComponentReport,
    ComponentStatus,
    InstallAllState,
    InstallAllStatus,
    InstallStep,
    InstallStepKind,
    InstallStepState,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.component_routes import ComponentRegistry
from _v3.setup_sequence import InstallSequence, SequenceFacts
from _v3.setup_service import SetupService
from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.setup_fixtures import FakeWorld, FixedTasks

S = InstallStepState
SINCE = "2026-11-20T00:00:00+00:00"


def _wait_until(predicate, timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, "等待条件超时"
        time.sleep(0.01)


class FakeTasks:
    """内存里的任务管理器替身：任务结果由测试用 finish(kind, state) 给出。"""

    def __init__(self) -> None:
        self.started: list[SetupTaskKind] = []
        self._tasks: dict[str, SetupTask] = {}
        self._lock = threading.Lock()

    def start(self, kind: SetupTaskKind) -> SetupTask:
        with self._lock:
            running = next((t for t in self._tasks.values() if t.kind == kind and t.state == SetupTaskState.running), None)
            if running is not None:
                return running
            self.started.append(kind)
            task = SetupTask(task_id=uuid.uuid4().hex, kind=kind, state=SetupTaskState.running, message="进行中",
                             started_at=SINCE)
            self._tasks[task.task_id] = task
            return task

    def get(self, task_id: str) -> SetupTask:
        with self._lock:
            return self._tasks[task_id]

    def finish(self, kind: SetupTaskKind, state: SetupTaskState, message: str = "") -> None:
        with self._lock:
            tid = next(t.task_id for t in self._tasks.values() if t.kind == kind and t.state == SetupTaskState.running)
            self._tasks[tid] = self._tasks[tid].model_copy(update={"state": state, "message": message})


class Facts:
    def __init__(self, **have: bool) -> None:
        self.have = {k: have.get(k, False) for k in ("ollama", "model", "runtime", "weights")}

    def build(self) -> SequenceFacts:
        return SequenceFacts(ollama_installed=lambda: self.have["ollama"], ollama_has_model=lambda: self.have["model"],
                             asr_runtime_installed=lambda: self.have["runtime"],
                             asr_weights_complete=lambda: self.have["weights"])


def _ollama(registry: ComponentRegistry, state: str, reason: str | None = None) -> None:
    others = [ComponentStatus(name=n, state="running", since=SINCE) for n in ("asr", "server")]
    registry.replace(ComponentReport(components=[ComponentStatus(name="ollama", state=state, reason=reason, since=SINCE), *others]))


def _sequence(tmp_path, tasks, facts, registry, **kw) -> InstallSequence:
    return InstallSequence(tasks=tasks, facts=facts.build(), components=registry, state_file=tmp_path / "install_all.json",
                           poll_s=0.01, **kw)


def _states(status) -> list[InstallStepState]:
    return [step.state for step in status.steps]


def _settled(seq: InstallSequence):
    return _wait_until(lambda: (s := seq.status()).state != InstallAllState.running and s)


def test_steps_run_in_order_and_skip_what_is_already_there(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    _ollama(registry, "running")
    seq = _sequence(tmp_path, tasks, Facts(ollama=True, model=True, runtime=True), registry)
    assert seq.start().state == InstallAllState.running
    _wait_until(lambda: tasks.started)
    assert tasks.started == [SetupTaskKind.asr_download] and seq.status().current == InstallStepKind.asr_download
    tasks.finish(SetupTaskKind.asr_download, SetupTaskState.succeeded, "已下载")
    done = _settled(seq)
    assert [s.kind for s in done.steps] == list(InstallStepKind)
    assert (done.state, done.current, _states(done)) == (InstallAllState.done, None, [S.skipped, S.done, S.skipped, S.skipped, S.done])
    assert done.steps[4].message == "已下载" and done.steps[4].task_id is not None


def test_wait_ollama_follows_component_snapshot(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    seq = _sequence(tmp_path / "a", tasks, Facts(ollama=True, model=True, runtime=True, weights=True), registry)
    seq.start()
    _wait_until(lambda: seq.status().steps[1].message == "等待 Ollama 运行")
    _ollama(registry, "stopped", "端口 17947 被 PID 7（ollama）占用，未抢占")
    failed = _settled(seq)
    assert (failed.state, failed.current, _states(failed)) == (InstallAllState.failed, InstallStepKind.wait_ollama,
                                                               [S.skipped, S.failed, S.pending, S.pending, S.pending])
    assert failed.steps[1].message == "Ollama 已停止：端口 17947 被 PID 7（ollama）占用，未抢占"

    _ollama(registry, "starting")
    slow = _sequence(tmp_path / "b", FakeTasks(), Facts(ollama=True), registry, wait_ollama_s=0.05)
    slow.start()
    timed_out = _settled(slow)
    assert timed_out.steps[1].message == "Ollama 未在 0.05 秒内运行：starting（无原因）"


def test_failed_task_stops_and_next_start_retries_from_that_step(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    _ollama(registry, "running")
    facts = Facts(ollama=True)
    seq = _sequence(tmp_path, tasks, facts, registry)
    seq.start()
    _wait_until(lambda: tasks.started)
    tasks.finish(SetupTaskKind.ollama_pull, SetupTaskState.interrupted, "安装进程已中断（服务重启或被结束），可重试")
    failed = _settled(seq)
    assert (failed.state, failed.current) == (InstallAllState.failed, InstallStepKind.ollama_pull)
    assert _states(failed) == [S.skipped, S.done, S.failed, S.pending, S.pending] and "已中断" in failed.steps[2].message
    assert tasks.started == [SetupTaskKind.ollama_pull]  # 后续步骤未执行

    facts.have.update(runtime=True, weights=True)
    assert seq.start().state == InstallAllState.running
    _wait_until(lambda: len(tasks.started) == 2)
    tasks.finish(SetupTaskKind.ollama_pull, SetupTaskState.succeeded, "qwen3.5:4b 已下载")
    done = _settled(seq)
    assert done.state == InstallAllState.done and _states(done) == [S.skipped, S.done, S.done, S.skipped, S.skipped]


def test_duplicate_start_returns_same_status_and_restart_resumes_running_sequence(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    _ollama(registry, "running")
    facts = Facts(ollama=True, model=True)
    seq = _sequence(tmp_path / "dup", tasks, facts, registry)
    seq.start()
    _wait_until(lambda: tasks.started)
    again = seq.start()
    assert again.state == InstallAllState.running and again.current == InstallStepKind.asr_runtime_install
    assert tasks.started == [SetupTaskKind.asr_runtime_install]
    tasks.finish(SetupTaskKind.asr_runtime_install, SetupTaskState.succeeded)
    _wait_until(lambda: len(tasks.started) == 2)
    tasks.finish(SetupTaskKind.asr_download, SetupTaskState.succeeded)
    assert _settled(seq).state == InstallAllState.done

    # 服务端重启：install_all.json 停在 running、当前步的 worker 仍在跑
    restarted, worker = FakeTasks(), None
    worker = restarted.start(SetupTaskKind.asr_runtime_install)
    steps = [InstallStep(kind=k, state=S.done if i < 3 else S.running if i == 3 else S.pending)
             for i, k in enumerate(InstallStepKind)]
    state_file = tmp_path / "restart" / "install_all.json"
    state_file.parent.mkdir()
    state_file.write_text(InstallAllStatus(state=InstallAllState.running, current=InstallStepKind.asr_runtime_install,
                                           steps=steps).model_dump_json(), encoding="utf-8")
    resumed = _sequence(tmp_path / "restart", restarted, facts, registry)
    _wait_until(lambda: resumed.status().steps[3].task_id == worker.task_id)
    restarted.finish(SetupTaskKind.asr_runtime_install, SetupTaskState.succeeded)
    _wait_until(lambda: len(restarted.started) == 2)
    assert restarted.started == [SetupTaskKind.asr_runtime_install, SetupTaskKind.asr_download]  # 沿用同一任务
    restarted.finish(SetupTaskKind.asr_download, SetupTaskState.succeeded)
    assert _settled(resumed).state == InstallAllState.done


def test_install_all_route_is_local_and_check_carries_status(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    _ollama(registry, "running")
    seq = _sequence(tmp_path / "seq", tasks, Facts(ollama=True, model=True, runtime=True, weights=True), registry)
    setup = SetupService(probes=FakeWorld().probes(), tasks=FixedTasks(), sequence=seq, components=registry)
    app = create_app(build_sandbox_runtime(root=tmp_path / "sb", token=TEST_TOKEN, setup=setup))
    assert lan_client(app).post("/api/setup/install-all", headers=lan_headers()).status_code == 403
    res = local_client(app).post("/api/setup/install-all", headers=local_headers())
    assert res.status_code == 202 and res.json()["state"] == "running"
    _settled(seq)
    body = local_client(app).get("/api/setup/check").json()["install_all"]
    assert body["state"] == "done" and [s["state"] for s in body["steps"]] == ["skipped", "done", "skipped", "skipped", "skipped"]
