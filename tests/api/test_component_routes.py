"""U4 组件状态上报与命令：整份替换、恰好三项、retry 经产品事件转给应用、仅本机；配置保存后发 config_changed。"""
from __future__ import annotations

from _contracts import (
    ComponentName,
    InstallAllState,
    InstallAllStatus,
    InstallStep,
    InstallStepKind,
    InstallStepState,
    SetupTaskKind,
)
from _v3.setup_service import SetupService
from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.setup_fixtures import FakeWorld, FixedSequence, FixedTasks, task

SINCE = "2026-11-20T00:00:00+00:00"


def _report(**states: str) -> dict:
    return {"components": [{"name": n, "state": states.get(n, "running"), "since": SINCE} for n in ("ollama", "asr", "server")]}


def _events(runtime) -> list[dict]:
    seen: list[dict] = []
    runtime.events.subscribe(lambda e: seen.append(e.model_dump(mode="json")))
    return seen


def test_report_replaces_whole_snapshot_and_requires_each_component_once(app, runtime):
    c = local_client(app)
    assert runtime.components.snapshot() == []
    res = c.post("/api/v1/components", json=_report(), headers=local_headers())
    assert res.status_code == 202 and res.json() == {"ok": True}
    res = c.post("/api/v1/components", json=_report(asr="stopped"), headers=local_headers())
    assert res.status_code == 202
    assert [(s.name, s.state) for s in runtime.components.snapshot()] == [
        (ComponentName.ollama, "running"), (ComponentName.asr, "stopped"), (ComponentName.server, "running")]
    partial = {"components": _report()["components"][:2]}
    doubled = {"components": [*_report()["components"], _report()["components"][0]]}
    for bad in (partial, doubled):
        res = c.post("/api/v1/components", json=bad, headers=local_headers())
        assert res.status_code == 400 and res.json()["error"] == "invalid_request"
    assert runtime.components.get(ComponentName.asr).state == "stopped"  # 坏上报不改已有快照


def test_retry_publishes_component_command_and_unknown_name_is_404(app, runtime):
    seen = _events(runtime)
    c = local_client(app)
    res = c.post("/api/v1/components/asr/retry", headers=local_headers())
    assert res.status_code == 202 and res.json() == {"ok": True}
    assert seen == [{"event": "component_command", "action": "retry", "component": "asr"}]
    res = c.post("/api/v1/components/whisper/retry", headers=local_headers())
    assert res.status_code == 404 and res.json()["error"] == "not_found"
    assert len(seen) == 1


def test_component_routes_are_local_only(app, runtime):
    c = lan_client(app)
    assert c.post("/api/v1/components", json=_report(), headers=lan_headers()).status_code == 403
    assert c.post("/api/v1/components/asr/retry", headers=lan_headers()).status_code == 403
    assert runtime.components.snapshot() == []


def test_config_save_publishes_config_changed_only_on_success(app, runtime):
    seen = _events(runtime)
    c = local_client(app)
    assert c.post("/api/v1/config", json={"autostart_enabled": False}, headers=local_headers()).status_code == 200
    assert seen == [{"event": "config_changed"}]
    assert c.post("/api/v1/config", json={"hotkey_voice": "ctrl+alt+x"}, headers=local_headers()).status_code == 400
    assert len(seen) == 1


def test_asr_retry_returns_409_during_step4_or_running_runtime_install_and_other_components_still_202(tmp_path):
    # 情况 1：一键装齐处于 Step 4（asr_runtime_install）运行中 → POST /api/v1/components/asr/retry 返 409，不发事件；ollama/server 仍 202
    steps = [
        InstallStep(kind=k, state=InstallStepState.running if k == InstallStepKind.asr_runtime_install else InstallStepState.pending)
        for k in InstallStepKind
    ]
    step4_running = InstallAllStatus(state=InstallAllState.running, current=InstallStepKind.asr_runtime_install, steps=steps)
    rt1 = build_sandbox_runtime(root=tmp_path / "sb1", token=TEST_TOKEN)
    rt1.setup = SetupService(
        probes=FakeWorld().probes(),
        tasks=FixedTasks(),
        sequence=FixedSequence(step4_running),
        components=rt1.components,
    )
    app1 = create_app(rt1)
    seen1 = _events(rt1)
    c1 = local_client(app1)
    res = c1.post("/api/v1/components/asr/retry", headers=local_headers())
    assert res.status_code == 409 and res.json()["error"] == "conflict"
    assert seen1 == []
    assert c1.post("/api/v1/components/ollama/retry", headers=local_headers()).status_code == 202
    assert c1.post("/api/v1/components/server/retry", headers=local_headers()).status_code == 202
    assert [e["component"] for e in seen1] == ["ollama", "server"]

    # 情况 2：即使 install_all 空闲，只要 asr_runtime_install 任务在跑也返 409；完成后恢复 202
    tasks_box = {SetupTaskKind.asr_runtime_install: task("running", "正在升级 CUDA", kind=SetupTaskKind.asr_runtime_install)}
    rt2 = build_sandbox_runtime(root=tmp_path / "sb2", token=TEST_TOKEN)
    fixed_tasks = FixedTasks(tasks_box)
    rt2.setup = SetupService(
        probes=FakeWorld().probes(),
        tasks=fixed_tasks,
        sequence=FixedSequence(),
        components=rt2.components,
    )
    app2 = create_app(rt2)
    seen2 = _events(rt2)
    c2 = local_client(app2)
    assert c2.post("/api/v1/components/asr/retry", headers=local_headers()).status_code == 409
    assert seen2 == []
    fixed_tasks._fixed[SetupTaskKind.asr_runtime_install] = task("succeeded", "完成", kind=SetupTaskKind.asr_runtime_install)
    assert c2.post("/api/v1/components/asr/retry", headers=local_headers()).status_code == 202
    assert seen2 == [{"event": "component_command", "action": "retry", "component": "asr"}]
