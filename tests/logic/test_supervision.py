"""U3 看护逻辑：组件规格、状态机（假时钟、假探针）、进程记录、端口占用。不拉起任何真实组件、不访问在用端口。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from _contracts import ComponentName, ComponentState
from _paths import AppPaths
from _platform.child_process import SessionRecord
from _ports import DEFAULT_ASR_PORT as ASR_PORT, DEFAULT_OLLAMA_PORT, DEFAULT_SERVER_PORT as SERVER_PORT
from _supervision import machine as sm
from _supervision.ports import port_owner
from _supervision.records import ChildrenRecord, clean_stale_children, read_record, write_record
from _supervision.spec import ReadyResult, build_specs


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r")


def _install_asr(paths: AppPaths, variant: str) -> None:
    paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.asr_manifest.write_text(json.dumps({"variant": variant}), encoding="utf-8")
    paths.asr_model_dir.mkdir(parents=True, exist_ok=True)
    (paths.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (paths.asr_model_dir / "model.safetensors").write_bytes(b"w")


def _specs(paths: AppPaths, **kw):
    return {s.name: s for s in build_specs(paths, {"PATH": "/usr/bin"}, **kw)}


# ---------------------------------------------------------------- spec
def test_specs_commands_env_ports_and_timeouts(paths):
    specs = _specs(paths)
    assert list(specs) == [ComponentName.ollama, ComponentName.asr, ComponentName.server]
    ollama, asr, server = specs.values()
    assert ollama.argv == (str(paths.ollama_binary), "serve") and ollama.port == DEFAULT_OLLAMA_PORT
    assert ollama.env["OLLAMA_HOST"] == f"127.0.0.1:{DEFAULT_OLLAMA_PORT}" and ollama.env["OLLAMA_MODELS"] == str(paths.ollama_models)
    assert asr.argv[:9] == (str(paths.asr_python), "-m", "runtimes.asr", "serve", "--host", "127.0.0.1", "--port", str(ASR_PORT), "--model")
    assert asr.env["PYTHONPATH"] == str(paths.program_root) and asr.env["HF_HUB_OFFLINE"] == "1" and asr.cwd == paths.program_root
    assert server.argv[:5] == (sys.executable, "-m", "uvicorn", "--factory", "app:create_production_app")
    assert server.argv[server.argv.index("--port") + 1] == str(SERVER_PORT) and str(paths.cert) in server.argv
    assert [(s.start_timeout_s, s.stop_timeout_s) for s in specs.values()] == [(60, 10), (300, 30), (60, 10)]
    assert {s.log_path for s in specs.values()} == {paths.logs / f"{n}.log" for n in ("ollama", "asr", "server")}
    assert server.installed() is None


def test_ports_can_be_moved_by_environment_for_all_three_components(paths):
    moved = {s.name: s for s in build_specs(paths, {"OVI_PORT_SERVER": "18945", "OVI_PORT_ASR": "18946",
                                                    "OVI_PORT_OLLAMA": "18947"})}
    assert moved[ComponentName.ollama].port == 18947 and moved[ComponentName.ollama].env["OLLAMA_HOST"] == "127.0.0.1:18947"
    assert moved[ComponentName.asr].port == 18946 and "18946" in moved[ComponentName.asr].argv
    assert moved[ComponentName.server].port == 18945 and "18945" in moved[ComponentName.server].argv
    with pytest.raises(ValueError, match="OVI_PORT_ASR"):
        build_specs(paths, {"OVI_PORT_ASR": "port"})


@pytest.mark.parametrize(("variant", "tail"), [
    ("cuda", ("--backend", "vllm", "--gpu-memory-utilization", "0.45", "--max-model-len", "4096")),
    ("cpu", ("--backend", "transformers", "--device", "cpu")),
    ("mps", ("--backend", "transformers", "--device", "mps")),
])
def test_asr_variant_comes_from_manifest_and_only_cuda_preflights(paths, variant, tail):
    _install_asr(paths, variant)
    smi_calls: list = []

    def run(cmd, **kw):
        smi_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "0, GPU-x, 16303, 1000, G\n", "")

    asr = _specs(paths, which=lambda n: "/usr/bin/nvidia-smi", run=run)[ComponentName.asr]
    assert asr.argv[-len(tail):] == tail and asr.installed() is None
    refused = asr.preflight()
    assert (refused is not None and "显存不足" in refused) if variant == "cuda" else (refused is None and not smi_calls)


def test_installed_is_read_from_files_each_time(paths):
    specs = _specs(paths)
    assert "不存在" in specs[ComponentName.ollama].installed()
    paths.ollama_binary.parent.mkdir(parents=True)
    paths.ollama_binary.write_bytes(b"")
    assert specs[ComponentName.ollama].installed() is None  # 同一规格，文件出现即已安装
    assert "manifest.json" in specs[ComponentName.asr].installed()
    _install_asr(paths, "cpu")
    (paths.asr_model_dir / "config.json").unlink()
    assert "config.json" in _specs(paths)[ComponentName.asr].installed()
    paths.asr_manifest.write_text('{"variant": "rocm"}', encoding="utf-8")
    assert "rocm" in _specs(paths)[ComponentName.asr].installed()


# ---------------------------------------------------------------- machine
def _fake(spec, *, ready=False, preflight=None):
    return replace(spec, ready=lambda: ReadyResult(ready, None if ready else "未响应"), preflight=lambda: preflight)


def test_not_installed_starts_once_files_appear_then_ready_runs(paths):
    spec = _fake(_specs(paths)[ComponentName.ollama], ready=True)
    m = sm.initial(ComponentName.ollama, 0.0)
    m, intents = sm.step(m, spec, sm.Probe(), 0.5)
    assert m.state == ComponentState.not_installed and "不存在" in m.reason and intents == ()
    paths.ollama_binary.parent.mkdir(parents=True)
    paths.ollama_binary.write_bytes(b"")
    m, intents = sm.step(m, spec, sm.Probe(), 1.0)
    assert m.state == ComponentState.starting and intents == (sm.Spawn(),)
    m, intents = sm.step(sm.spawned(m, 4321), spec, sm.Probe(), 1.5)
    assert m.state == ComponentState.running and m.pid == 4321 and intents == ()


@pytest.mark.parametrize(("name", "channel"), [(ComponentName.asr, "error"), (ComponentName.server, "tray")])
def test_failures_terminate_session_back_off_then_stop_once_with_routed_notice(paths, name, channel):
    spec = replace(_fake(_specs(paths)[name]), installed=lambda: None)
    m, now = sm.initial(name, 0.0), 0.0
    delays = []
    for attempt in range(1, sm.MAX_CONSECUTIVE_FAILURES + 1):
        m, intents = sm.step(m, spec, sm.Probe(), now)
        assert m.state == ComponentState.starting and intents == (sm.Spawn(),)
        now += 1
        m, intents = sm.step(sm.spawned(m, 100 + attempt), spec, sm.Probe(exit_code=1), now)
        assert intents[0] == sm.TerminateSession() and m.failures == attempt and m.pid is None
        if m.state == ComponentState.backing_off:
            delays.append(m.retry_at - now)
            assert sm.step(m, spec, sm.Probe(), m.retry_at - 0.1)[0].state == ComponentState.backing_off
            now = m.retry_at
    assert delays == list(sm.BACKOFF_S)
    assert m.state == ComponentState.stopped and "连续失败 5 次" in m.reason and str(spec.log_path) in m.reason
    assert intents[1:] == (sm.Notify(channel, m.reason),)
    assert sm.step(m, spec, sm.Probe(), now + 999) == (m, ())  # 只通知一次，不再自动拉起
    m = sm.retry(m, now + 1000)
    m, intents = sm.step(m, spec, sm.Probe(), now + 1000)
    assert m.state == ComponentState.starting and m.failures == 0 and intents == (sm.Spawn(),)


def test_start_timeout_fails_with_last_ready_detail_and_running_60s_resets(paths):
    spec = replace(_fake(_specs(paths)[ComponentName.ollama]), installed=lambda: None)
    m, _ = sm.step(sm.initial(ComponentName.ollama, 0.0), spec, sm.Probe(), 0.0)
    m, _ = sm.step(m, spec, sm.Probe(), 59.0)
    assert m.state == ComponentState.starting
    m, intents = sm.step(m, spec, sm.Probe(), 60.0)
    assert m.state == ComponentState.backing_off and "启动超时 60s：未响应" in m.reason and intents == (sm.TerminateSession(),)
    m, _ = sm.step(m, spec, sm.Probe(), m.retry_at)
    m, _ = sm.step(m, replace(spec, ready=lambda: ReadyResult(True)), sm.Probe(), 63.0)
    assert m.state == ComponentState.running and m.failures == 1
    assert sm.step(m, spec, sm.Probe(), 63.0 + sm.STABLE_RESET_S - 1)[0].failures == 1
    assert sm.step(m, spec, sm.Probe(), 63.0 + sm.STABLE_RESET_S)[0].failures == 0


def test_port_taken_or_preflight_refused_stops_without_counting_failure(paths):
    spec = replace(_fake(_specs(paths)[ComponentName.asr]), installed=lambda: None)
    m, intents = sm.step(sm.initial(ComponentName.asr, 0.0), spec, sm.Probe(port_owner=lambda: "PID 7（ollama）"), 1.0)
    assert m.state == ComponentState.stopped and m.failures == 0 and intents == (sm.Notify("error", m.reason),)
    assert m.reason == f"端口 {ASR_PORT} 被 PID 7（ollama）占用，未抢占"
    m, intents = sm.step(sm.retry(m, 2.0), replace(spec, preflight=lambda: "显存不足：x"), sm.Probe(), 2.0)
    assert m.state == ComponentState.stopped and m.reason == "显存不足：x" and intents == (sm.Notify("error", "显存不足：x"),)


def test_app_exit_stops_without_notice_and_status_times_are_wall_clock(paths):
    spec = replace(_fake(_specs(paths)[ComponentName.server]), installed=lambda: None)
    m, _ = sm.step(sm.initial(ComponentName.server, 10.0), spec, sm.Probe(), 10.0)
    m, intents = sm.stop(sm.spawned(m, 55), 20.0)
    assert (m.state, m.reason, intents) == (ComponentState.stopped, sm.APP_EXIT_REASON, (sm.TerminateSession(),))
    status = sm.to_status(m, mono_now=30.0, wall_now=1_000_000.0)
    assert datetime.fromisoformat(status.since).timestamp() == 1_000_000.0 - 10.0 and status.pid is None


# ---------------------------------------------------------------- records
def test_records_roundtrip_and_stale_cleanup(tmp_path):
    path = tmp_path / "run" / "children.json"
    record = ChildrenRecord(app_pid=10, app_identity="a1", children={"asr": SessionRecord(pid=20, sid=20, leader_starttime="s1")})
    write_record(path, record)
    assert read_record(path) == record and (path.stat().st_mode & 0o777) == 0o600
    with pytest.raises(RuntimeError, match="仍在运行"):
        clean_stale_children(path, identity=lambda pid: "a1", kill=lambda r: pytest.fail("不得清理"))
    killed = clean_stale_children(path, identity=lambda pid: None, kill=lambda r: [r.pid, 21])
    assert killed == [20, 21] and not path.exists()
    assert clean_stale_children(path) == []
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="children.json"):
        read_record(path)


# ---------------------------------------------------------------- ports
def test_port_owner_names_the_listening_process():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert port_owner(port, os_name="linux") == f"PID {os.getpid()}（{Path('/proc/self/comm').read_text().strip()}）"
    assert port_owner(port, os_name="linux") is None


@pytest.mark.parametrize(("os_name", "outputs", "expected"), [
    ("macos", {"lsof": "p321\ncollama\n"}, "PID 321（ollama）"),
    ("windows", {"netstat": "  TCP    0.0.0.0:17947   0.0.0.0:0   LISTENING   321\n", "tasklist": '"ollama.exe","321","Console"\n'},
     "PID 321（ollama.exe）"),
    ("macos", {"lsof": ""}, "未能识别占用进程（lsof 退出码 1）"),
])
def test_port_owner_on_macos_and_windows(os_name, outputs, expected):
    def run(cmd, **kw):
        out = outputs.get(cmd[0], "")
        return subprocess.CompletedProcess(cmd, 0 if out else 1, out, "")

    assert port_owner(17947, os_name=os_name, run=run, in_use=lambda p: True) == expected
