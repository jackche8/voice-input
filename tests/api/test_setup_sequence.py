"""U5b 一键装齐：五步顺序与按事实跳过；wait_ollama 读组件快照；任务失败停步；再次开始从失败步重试；
新实例读 install_all.json 恢复；运行中重复开始返回同一状态；POST /api/setup/install-all 仅本机。"""
from __future__ import annotations

import threading
import time
import uuid

from _contracts import (
    ComponentName,
    ComponentReport,
    ComponentState,
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
from _events import EventBus
from _supervision.machine import UPGRADE_STOP_REASON
from _v3.component_routes import ComponentRegistry
from _v3.runtime_install import AsrHardwareProbe
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

    def latest(self, kind: SetupTaskKind) -> SetupTask | None:
        with self._lock:
            same = [t for t in self._tasks.values() if t.kind == kind]
            return same[-1] if same else None

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


def _report_states(registry: ComponentRegistry, *, ollama="running", asr="running", asr_reason=None, asr_pid=None) -> None:
    registry.replace(
        ComponentReport(
            components=[
                ComponentStatus(name="ollama", state=ollama, since=SINCE),
                ComponentStatus(name="asr", state=asr, reason=asr_reason, pid=asr_pid, since=SINCE),
                ComponentStatus(name="server", state="running", since=SINCE),
            ]
        )
    )


def test_start_concurrent_double_click_spawns_runner_once(tmp_path):
    registry, tasks = ComponentRegistry(), FakeTasks()
    _ollama(registry, "running")
    spawned: list = []
    barrier = threading.Barrier(4)
    seq = InstallSequence(
        tasks=tasks,
        facts=Facts(ollama=True, model=True, runtime=True, weights=True).build(),
        components=registry,
        state_file=tmp_path / "race" / "install_all.json",
        spawn=spawned.append,
        poll_s=0.01,
    )

    def hit() -> None:
        barrier.wait()
        seq.start()

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)
    assert len(spawned) == 1


def test_step4_safe_upgrade_order_early_abort_recovery_and_post_step5_restart(tmp_path):
    # 1) 旧驱动 < 570：实时探测直接失败，绝不发 stop、不启 worker
    reg1, tasks1, ev1 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg1, asr="running", asr_pid=101)
    cmds1: list[tuple[str, str]] = []
    ev1.subscribe(lambda e: cmds1.append((e.action, e.component.value)) if hasattr(e, "action") else None)
    invalidations = {"n": 0}
    facts1 = SequenceFacts(
        ollama_installed=lambda: True,
        ollama_has_model=lambda: True,
        asr_runtime_installed=lambda: True,
        asr_weights_complete=lambda: True,
        asr_hardware_probe=lambda: AsrHardwareProbe(None, "驱动过旧", error="请升级 NVIDIA 驱动到 570 及以上"),
        asr_installed_variant=lambda: "cpu",
        asr_runtime_on_disk=lambda: True,
        asr_disk_shortfall=lambda _v: None,
        invalidate_hardware_cache=lambda: invalidations.__setitem__("n", invalidations["n"] + 1),
    )
    seq1 = InstallSequence(tasks=tasks1, facts=facts1, components=reg1, events=ev1, state_file=tmp_path / "drv.json", poll_s=0.01)
    seq1.start()
    res1 = _settled(seq1)
    assert res1.state == InstallAllState.failed and res1.current == InstallStepKind.asr_runtime_install
    assert "570" in res1.steps[3].message and cmds1 == [] and tasks1.started == []
    assert invalidations["n"] == 2  # 进入与退出 Step 4 均失效硬件缓存

    # 2) 磁盘不足：先于停服直接失败，绝不发 stop、不启 worker
    reg2, tasks2, ev2 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg2, asr="running", asr_pid=102)
    cmds2: list[tuple[str, str]] = []
    ev2.subscribe(lambda e: cmds2.append((e.action, e.component.value)) if hasattr(e, "action") else None)
    facts2 = SequenceFacts(
        ollama_installed=lambda: True,
        ollama_has_model=lambda: True,
        asr_runtime_installed=lambda: True,
        asr_weights_complete=lambda: True,
        asr_hardware_probe=lambda: AsrHardwareProbe("cuda", "RTX 4070"),
        asr_installed_variant=lambda: "cpu",
        asr_runtime_on_disk=lambda: True,
        asr_disk_shortfall=lambda target: f"磁盘空间不足（{target}）",
    )
    seq2 = InstallSequence(tasks=tasks2, facts=facts2, components=reg2, events=ev2, state_file=tmp_path / "disk.json", poll_s=0.01)
    seq2.start()
    res2 = _settled(seq2)
    assert res2.state == InstallAllState.failed and "磁盘空间不足（cuda）" in res2.steps[3].message
    assert cmds2 == [] and tasks2.started == []

    # 3) 停服早停条件：仅 (stopped, pid is not None, "无法结束组件进程" in reason) 立即中止，绝不启 worker
    reg3, tasks3, ev3 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg3, asr="running", asr_pid=303)
    ev3.subscribe(
        lambda e: _report_states(
            reg3, asr="stopped", asr_pid=303, asr_reason=f"{UPGRADE_STOP_REASON}；无法结束组件进程：RuntimeError: stuck"
        )
        if getattr(e, "action", None) == "stop"
        else None
    )
    facts3 = SequenceFacts(
        ollama_installed=lambda: True,
        ollama_has_model=lambda: True,
        asr_runtime_installed=lambda: True,
        asr_weights_complete=lambda: True,
        asr_hardware_probe=lambda: AsrHardwareProbe("cuda", "RTX 4070"),
        asr_installed_variant=lambda: "cpu",
        asr_runtime_on_disk=lambda: True,
        asr_disk_shortfall=lambda _v: None,
    )
    seq3 = InstallSequence(
        tasks=tasks3, facts=facts3, components=reg3, events=ev3, state_file=tmp_path / "stuck.json", poll_s=0.01, stop_asr_s=5.0
    )
    seq3.start()
    res3 = _settled(seq3)
    assert res3.state == InstallAllState.failed and "无法结束组件进程" in res3.steps[3].message and tasks3.started == []

    # 4) 停服后 worker 失败 → Step 4 ⑥ 自动补发 retry 恢复旧 ASR
    reg4, tasks4, ev4 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg4, asr="running", asr_pid=404)
    cmds4: list[tuple[str, str]] = []

    def on_ev4(e) -> None:
        if getattr(e, "action", None) == "stop":
            cmds4.append(("stop", e.component.value))
            _report_states(reg4, asr="stopped", asr_pid=None, asr_reason=UPGRADE_STOP_REASON)
        elif getattr(e, "action", None) == "retry":
            cmds4.append(("retry", e.component.value))

    ev4.subscribe(on_ev4)
    seq4 = InstallSequence(tasks=tasks4, facts=facts3, components=reg4, events=ev4, state_file=tmp_path / "fail4.json", poll_s=0.01)
    seq4.start()
    _wait_until(lambda: tasks4.started == [SetupTaskKind.asr_runtime_install])
    tasks4.finish(SetupTaskKind.asr_runtime_install, SetupTaskState.failed, "网络断开")
    res4 = _settled(seq4)
    assert res4.state == InstallAllState.failed and cmds4 == [("stop", "asr"), ("retry", "asr")]

    # 5) 完整 cpu → cuda 成功升级：先停 ASR → 跑 worker → 跳过已有权重 → 落盘 done 前幂等重发 retry 直至离态
    reg5, tasks5, ev5 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg5, asr="running", asr_pid=505)
    cmds5: list[tuple[str, str]] = []
    retry_hits = {"n": 0}

    def on_ev5(e) -> None:
        action = getattr(e, "action", None)
        if action == "stop":
            cmds5.append(("stop", e.component.value))
            _report_states(reg5, asr="stopped", asr_pid=None, asr_reason=UPGRADE_STOP_REASON)
        elif action == "retry":
            cmds5.append(("retry", e.component.value))
            retry_hits["n"] += 1
            if retry_hits["n"] >= 2:  # 第 1 次假装看护层未收到，第 2 次重发后离态
                _report_states(reg5, asr="starting", asr_pid=606)

    ev5.subscribe(on_ev5)
    seq5 = InstallSequence(
        tasks=tasks5,
        facts=facts3,
        components=reg5,
        events=ev5,
        state_file=tmp_path / "ok5.json",
        poll_s=0.01,
        restart_asr_s=5.0,
        restart_asr_resend_s=0.02,
    )
    seq5.start()
    _wait_until(lambda: tasks5.started == [SetupTaskKind.asr_runtime_install])
    tasks5.finish(SetupTaskKind.asr_runtime_install, SetupTaskState.succeeded, "CUDA 已安装")
    res5 = _settled(seq5)
    assert res5.state == InstallAllState.done and res5.steps[3].state == InstallStepState.done
    assert retry_hits["n"] >= 2 and reg5.get(ComponentName.asr).state == ComponentState.starting
    assert cmds5[0] == ("stop", "asr") and cmds5[1] == ("retry", "asr")

    # 6) 已有 running worker（服务端重启恢复）：Step 4 ① 直接复用，不重停 ASR、不重查磁盘
    reg6, tasks6, ev6 = ComponentRegistry(), FakeTasks(), EventBus()
    _report_states(reg6, asr="stopped", asr_pid=None, asr_reason=UPGRADE_STOP_REASON)
    running_task = tasks6.start(SetupTaskKind.asr_runtime_install)
    probe_called = {"n": 0}
    facts6 = SequenceFacts(
        ollama_installed=lambda: True,
        ollama_has_model=lambda: True,
        asr_runtime_installed=lambda: False,
        asr_weights_complete=lambda: True,
        asr_hardware_probe=lambda: probe_called.__setitem__("n", probe_called["n"] + 1) or AsrHardwareProbe("cuda", "x"),
        asr_installed_variant=lambda: None,
        asr_runtime_on_disk=lambda: True,
        asr_disk_shortfall=lambda _v: "不应触发磁盘检查",
    )
    ev6.subscribe(lambda e: _report_states(reg6, asr="starting") if getattr(e, "action", None) == "retry" else None)
    seq6 = InstallSequence(tasks=tasks6, facts=facts6, components=reg6, events=ev6, state_file=tmp_path / "resume6.json", poll_s=0.01)
    seq6.start()
    _wait_until(lambda: seq6.status().steps[3].task_id == running_task.task_id)
    assert probe_called["n"] == 0 and tasks6.started == [SetupTaskKind.asr_runtime_install]
    tasks6.finish(SetupTaskKind.asr_runtime_install, SetupTaskState.succeeded, "续跑完成")
    assert _settled(seq6).state == InstallAllState.done
