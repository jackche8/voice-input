"""生产工厂隔离装配：同一 compose_production_runtime + create_app(static/v3)，

隔离根目录 + 假令牌 + httpx.MockTransport（假 Ollama / 假 sidecar 主机名，不开任何套接字）+ 假进程。
验证：启动恢复、配置迁移、真实适配器接线、设置作业、局域网签发、激活门闩。不是官方模型或真机验收。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from _asr_provider import SidecarStreamProvider
from _contracts import SetupTaskKind
from _paths import PROGRAM_ROOT, STATIC_V3, AppPaths
from _v3.production import (
    ProductionEndpoints,
    SystemAdapters,
    compose_production_runtime,
    setup_task_actions,
)
from _v3_app import create_app
from tests.api.providers import make_text_wav

FAKE_TOKEN = "test-production-factory-token"
ENDPOINTS = ProductionEndpoints(sidecar_url="http://sidecar.fixture", ollama_url="http://ollama.fixture")


class FakeServices:
    """假 Ollama + 假 ASR sidecar。记录请求，供断言真实适配器的协议。"""

    def __init__(self) -> None:
        self.models: list[str] = []
        self.sidecar_loaded = False
        self.requests: list[tuple[str, str]] = []
        self.chat_payloads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.requests.append((host, path))
        if host == "ollama.fixture":
            if path == "/api/version":
                return httpx.Response(200, json={"version": "0.40.16"})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": m, "model": m, "size": 3_300_000_000} for m in self.models]})
            if path == "/api/pull":
                self.models.append(json.loads(request.content)["model"])
                lines = [{"status": "pulling x", "digest": "d", "total": 10, "completed": 10}, {"status": "success"}]
                return httpx.Response(200, content="\n".join(map(json.dumps, lines)).encode())
            if path == "/api/chat":
                payload = json.loads(request.content)
                self.chat_payloads.append(payload)
                raw_msg = payload["messages"][-1]["content"]
                if "<asr_transcript>" in raw_msg and "</asr_transcript>" in raw_msg:
                    inner = raw_msg.split("<asr_transcript>", 1)[1].split("</asr_transcript>", 1)[0].strip()
                else:
                    inner = raw_msg.split("\n")[-1]
                text = inner.replace("错字", "正字")
                return httpx.Response(200, json={"message": {"content": text}})
        if host == "sidecar.fixture":
            if path == "/health":
                return httpx.Response(200, json={"ok": True, "loaded": self.sidecar_loaded})
            if path == "/v1/transcribe":
                return httpx.Response(200, json={"ok": True, "text": "今天天气错字", "language": "zh"})
        return httpx.Response(404, json={"error": f"fixture 未定义 {host}{path}"})


class FakeRun:
    def __init__(self, services: FakeServices) -> None:
        self.calls: list[list[str]] = []
        self.services = services

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if cmd[0] == "nvidia-smi":  # 假单卡读数：16303 MiB 总量、14574 MiB 空闲（本机 06:3x EST 只读读数）
            return subprocess.CompletedProcess(cmd, 0, stdout="0, GPU-fixture, 16303, 14574, Fixture GPU\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _paths_for(root: Path) -> AppPaths:
    return AppPaths.for_roots(config_root=root / "config", data_root=root / "data", runtime_dir=root / "run")


def _seed(paths: AppPaths) -> None:
    paths.config_root.mkdir(parents=True)
    paths.data_root.mkdir(parents=True)
    paths.config.write_text(json.dumps({
        "ollama_model": "qwen2.5:7b",
        "hotkey_voice": "ctrl+alt+z",
        "custom_keywords": ["专名甲"],
        "translate_target_lang": "en",
        "baidu_appid": "fixture-appid",
        "baidu_appkey": "fixture-secret",
        "whisper_model": "large-v3",
    }), encoding="utf-8")
    claimed = {
        "job_id": "job-claimed", "state": "claimed", "text": "旧句", "source": "desktop", "focus_id": "x11:1",
        "claim_id": "c1", "created_at": "2026-11-10T00:00:00+00:00", "updated_at": "2026-11-10T00:00:00+00:00",
    }
    paths.jobs.write_text(json.dumps({"version": 1, "jobs": {"job-claimed": claimed}}), encoding="utf-8")


class WorkerSpy:
    """记录服务端拉起的安装 worker 命令行；不真起进程（动作本身在下面用同一动作表就地执行）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(pid=999_999, returncode=None, poll=lambda: None)


@pytest.fixture
def world(tmp_path: Path):
    paths = _paths_for(tmp_path / "prod-root")
    _seed(paths)
    services = FakeServices()
    run = FakeRun(services)
    workers = WorkerSpy()
    runners: list = []  # 一键装齐的编排线程：只记录不运行（其编排行为见 tests/api/test_setup_sequence.py）
    adapters = SystemAdapters(
        environ={"XDG_SESSION_TYPE": "wayland"},
        run=run,
        popen=workers,
        which=lambda name: f"/usr/bin/{name}" if name in ("xclip", "nvidia-smi") else None,
        portal_version=lambda iface: (1, None),
        ocr_languages=lambda: ["eng", "chi_sim", "chi_tra"],
        keyboard_devices=lambda: ["/dev/input/event5"],  # Wayland 环境：不碰真实输入设备
        list_ips=lambda: ["192.168.10.20"],
        http_transport=httpx.MockTransport(services),
        spawn=runners.append,
    )
    runtime = compose_production_runtime(paths=paths, token=FAKE_TOKEN, adapters=adapters, endpoints=ENDPOINTS)
    app = create_app(runtime, static_v3=STATIC_V3)
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945")
    yield {"runtime": runtime, "client": client, "services": services, "run": run, "paths": paths,
           "adapters": adapters, "workers": workers, "runners": runners}
    client.close()


H = {"Origin": "https://127.0.0.1:17945"}


def test_production_compose_requires_explicit_paths():
    with pytest.raises(TypeError):
        compose_production_runtime()  # type: ignore[call-arg]


def test_startup_recovers_claimed_jobs_without_redelivery(world):
    doc = json.loads(world["paths"].jobs.read_text(encoding="utf-8"))
    job = doc["jobs"]["job-claimed"]
    assert job["state"] == "delivery_unknown"
    claim = world["client"].post("/api/v1/jobs/claim", json={}, headers=H)
    assert claim.status_code == 200
    assert not claim.json() or claim.json().get("job_id") != "job-claimed", "恢复后不得自动重投"


def test_config_migrated_keeps_user_fields_and_hides_secrets(world):
    disk = json.loads(world["paths"].config.read_text(encoding="utf-8"))
    assert disk["ollama_model"] == "qwen3.5:4b" and disk["legacy_ollama_model"] == "qwen2.5:7b"
    assert disk["custom_keywords"] == ["专名甲"] and disk["translate_target_lang"] == "en"
    assert disk["baidu_appkey"] == "fixture-secret" and disk["hotkey_screenshot"] == "ctrl+alt+x"
    view = world["client"].get("/api/v1/config").json()
    assert "fixture-secret" not in json.dumps(view) and view["ollama_model"] == "qwen3.5:4b"


def test_real_adapters_drive_from_audio_polish_and_queue(world):
    client = world["client"]
    assert client.post("/api/v1/config", json={"polish_enabled": True}, headers=H).status_code == 200
    res = client.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "job-web-1"},
        files={"audio": ("a.wav", make_text_wav("audio-fixture"), "audio/wav")},
        headers=H,
    )
    assert res.status_code == 200, res.text
    assert res.json()["text"] == "今天天气正字"
    payload = world["services"].chat_payloads[-1]
    assert payload["model"] == "qwen3.5:4b" and payload["think"] is False
    assert "专名甲" in payload["messages"][-1]["content"]
    assert ("sidecar.fixture", "/v1/transcribe") in world["services"].requests
    assert isinstance(world["runtime"].stream_provider, SidecarStreamProvider)
    stt = client.post("/api/v1/stt", files={"audio": ("a.wav", make_text_wav("audio-stt"), "audio/wav")}, headers=H)
    assert stt.json()["text"] == "今天天气错字", "公开 STT 不润色"


def test_setup_check_and_real_actions_via_fakes(world):
    client = world["client"]
    check = client.get("/api/setup/check").json()
    assert check["session_type"] == "wayland" and check["components"] == [] and check["ollama"]["running"] is False
    assert check["asr"]["status"] == "missing" and check["install_all"]["state"] == "idle"
    caps = {c["id"]: c for c in check["capabilities"]}
    assert caps["hotkey_conflicts"]["available"] is False and "gsettings 不可用" in caps["hotkey_conflicts"]["detail"]
    assert "desktop_daemon" not in caps

    # 应用上报组件快照后，设置检查与一键装齐读的是同一份缓存
    report = {"components": [{"name": n, "state": "running", "since": "2026-11-20T00:00:00+00:00"} for n in ("ollama", "asr", "server")]}
    assert client.post("/api/v1/components", json=report, headers=H).status_code == 202
    check = client.get("/api/setup/check").json()
    assert check["ollama"]["running"] and check["ollama"]["model_status"] == "missing" and check["asr"]["sidecar_running"]

    install_all = client.post("/api/setup/install-all", headers=H)
    assert install_all.status_code == 202 and install_all.json()["state"] == "running" and len(world["runners"]) == 1
    assert (world["paths"].data_root / "setup" / "install_all.json").is_file()

    started = world["runtime"].setup.tasks.start(SetupTaskKind.ollama_pull)
    assert started.state == "running"
    (argv, kwargs), = world["workers"].calls
    task_file = world["paths"].setup_tasks / f"{started.task_id}.json"
    assert argv == [sys.executable, "-m", "_v3.setup_worker", "--task-file", str(task_file), "--kind", "ollama_pull",
                    "--owner-pid", str(os.getppid()), "--owner-identity", argv[-1]]
    assert kwargs["start_new_session"] is True and kwargs["cwd"] == str(PROGRAM_ROOT)

    # worker 进程执行的是同一张动作表：就地执行验证真实动作接线（假 Ollama）
    actions = setup_task_actions(world["paths"], ENDPOINTS, world["adapters"], world["runtime"].config)
    assert set(actions) == set(SetupTaskKind)
    assert actions[SetupTaskKind.ollama_pull](lambda p: None).endswith("已下载")
    assert client.get("/api/setup/check").json()["ollama"]["model_status"] == "ready"


def test_lan_access_uses_explicit_token(world):
    body = world["client"].get("/api/v1/lan-access").json()
    assert body["token"] == FAKE_TOKEN
    assert body["entries"][0]["url"].startswith("https://192.168.10.20:17945/voice#token=")


def test_pages_served(world):
    assert world["client"].get("/setup").status_code == 200
