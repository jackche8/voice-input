"""U4 组件状态上报与命令：整份替换、恰好三项、retry 经产品事件转给应用、仅本机；配置保存后发 config_changed。"""
from __future__ import annotations

from _contracts import ComponentName
from tests.api.conftest import lan_client, lan_headers, local_client, local_headers

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
