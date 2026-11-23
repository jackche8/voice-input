"""生产工厂隔离装配：同一 compose_production_runtime + create_app(static/v3)，

隔离根目录 + 假令牌 + httpx.MockTransport（假 Ollama / 假 sidecar 主机名，不开任何套接字）+ 假进程。
验证：启动恢复、配置迁移、真实适配器接线、设置作业、局域网签发、激活门闩。不是官方模型或真机验收。
"""
from __future__ import annotations

import io
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from _asr_provider import SidecarStreamProvider
from _config import ConfigStore
from _contracts import (
    AudioInputDevice,
    AudioInputListResponse,
    ErrorCode,
    LogicError,
    OcrTextBlock,
    OcrTextLine,
    SetupTaskKind,
    SetupTaskState,
)
from _paths import INTERNAL_SETUP_WORKER_FLAG, PROGRAM_ROOT, STATIC_V3, AppPaths, default_app_paths
from _platform.child_process import process_identity
from _v3 import setup_actions as setup_actions_mod
from _v3.production import (
    ProductionEndpoints,
    SystemAdapters,
    compose_production_runtime,
    setup_task_actions,
    setup_task_kinds,
    setup_worker_kind,
)
from _v3.runtime_install import probe_asr_hardware
from _v3.setup_tasks import SetupTaskManager
from _v3.setup_worker import APP_EXIT_MESSAGE
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
                if "<source_text>" in raw_msg and "</source_text>" in raw_msg:
                    inner = raw_msg.split("<source_text>", 1)[1].split("</source_text>", 1)[0].strip()
                    if isinstance(payload.get("format"), dict) and payload["format"].get("type") == "array":
                        items = json.loads(inner)
                        out = json.dumps([f"tr:{item}" for item in items], ensure_ascii=False)
                        return httpx.Response(200, json={"message": {"content": out}, "done_reason": "stop"})
                    return httpx.Response(200, json={"message": {"content": f"tr:{inner}"}, "done_reason": "stop"})
                if "<asr_transcript>" in raw_msg and "</asr_transcript>" in raw_msg:
                    inner = raw_msg.split("<asr_transcript>", 1)[1].split("</asr_transcript>", 1)[0].strip()
                else:
                    inner = raw_msg.split("\n")[-1]
                text = inner.replace("错字", "正字")
                return httpx.Response(200, json={"message": {"content": text}, "done_reason": "stop"})
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
        if Path(cmd[0]).name.lower() in ("nvidia-smi", "nvidia-smi.exe"):
            return subprocess.CompletedProcess(cmd, 0, stdout="0, GPU-fixture, 16303, 14574, 572.16, 8.9, Fixture GPU\n", stderr="")
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
        list_audio_inputs=lambda: AudioInputListResponse(
            default_device="Fixture USB Mic",
            devices=[AudioInputDevice(name="Fixture USB Mic", hostapi="PulseAudio", is_default=True)],
        ),
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
    assert "translate_target_lang" not in view
    assert view["translate_lang_pair"] == "zh-en" and view["translate_auto_enabled"] is True
    assert view["ui_theme"] == "light" and view["audio_input_device"] == ""


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


def test_production_audio_inputs_wired_and_lazy_default_binds_os_name(world, tmp_path, monkeypatch):
    import _v3.production as prod_mod

    # 1. 注入夹具走真实路由分发返回 200
    resp = world["client"].get("/api/v1/audio-inputs")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "default_device": "Fixture USB Mic",
        "devices": [{"name": "Fixture USB Mic", "hostapi": "PulseAudio", "is_default": True}],
    }

    # 2. 默认 SystemAdapters(list_audio_inputs=None) 构造期绝不触发枚举，请求时才延迟传 refresh=True 与 paths.os_name
    seen: list[tuple[bool, str | None]] = []

    def _spy_list(*, refresh: bool = True, os_name: str | None = None, **_kw) -> AudioInputListResponse:
        seen.append((refresh, os_name))
        return AudioInputListResponse(
            default_device="Win Lazy Mic",
            devices=[AudioInputDevice(name="Win Lazy Mic", hostapi="Windows DirectSound", is_default=True)],
        )

    monkeypatch.setattr(prod_mod, "list_audio_input_devices", _spy_list)
    win_paths = AppPaths.for_roots(
        config_root=tmp_path / "lazy-win" / "c",
        data_root=tmp_path / "lazy-win" / "d",
        runtime_dir=tmp_path / "lazy-win" / "r",
        os_name="windows",
        environ={"APPDATA": str(tmp_path / "a"), "LOCALAPPDATA": str(tmp_path / "l")},
    )
    lazy_rt = compose_production_runtime(
        paths=win_paths,
        token=FAKE_TOKEN,
        adapters=SystemAdapters(environ={}, http_transport=httpx.MockTransport(FakeServices())),
        endpoints=ENDPOINTS,
    )
    assert seen == [], "compose_production_runtime 构造期不得主动触发声卡枚举"
    with TestClient(
        create_app(lazy_rt, static_v3=STATIC_V3),
        client=("127.0.0.1", 50000),
        base_url="https://127.0.0.1:17945",
    ) as client:
        out = client.get("/api/v1/audio-inputs")
        assert out.status_code == 200, out.text
        assert out.json()["default_device"] == "Win Lazy Mic"
    assert seen == [(True, "windows")]


def test_real_translate_and_translate_blocks_adapters_wire_new_contract(world):
    client = world["client"]
    resp = client.post("/api/v1/translate", json={"text": "本地翻译测试", "lang_pair": "zh-en"}, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["lang_pair"], body["source_lang"], body["target_lang"], body["direction_mode"], body["chunks"]) == (
        "zh-en",
        "zh",
        "en",
        "auto",
        1,
    )
    assert body["text"] == "tr:本地翻译测试"

    blocks = [
        OcrTextBlock(
            bbox=(0, 0, 80, 20), ocr_text="你好世界", translated_text="",
            lines=[OcrTextLine(bbox=(0, 0, 80, 20), text="你好世界")],
        ),
        OcrTextBlock(
            bbox=(0, 24, 40, 20), ocr_text="42%", translated_text="",
            lines=[OcrTextLine(bbox=(0, 24, 40, 20), text="42%")],
        ),
    ]
    out = world["runtime"].translate_blocks(blocks, source_lang="zh", target_lang="en", lang_pair="zh-en")
    assert [b.translated_text for b in out] == ["tr:你好世界", "42%"]
    batch_payload = world["services"].chat_payloads[-1]
    assert "源语言: 中文" in batch_payload["messages"][-1]["content"]
    assert "目标语言: 英文" in batch_payload["messages"][-1]["content"]


def test_production_setup_worker_uses_internal_flag_when_frozen(world, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/app/ollama-voice-input")
    runtime = compose_production_runtime(
        paths=world["paths"], token=FAKE_TOKEN, adapters=world["adapters"], endpoints=ENDPOINTS,
    )
    started = runtime.setup.tasks.start(SetupTaskKind.ollama_pull)
    assert started.state == "running"
    argv, _kwargs = world["workers"].calls[-1]
    assert argv[:2] == ["/app/ollama-voice-input", INTERNAL_SETUP_WORKER_FLAG]
    assert "--task-file" in argv and "--kind" in argv and "ollama_pull" in argv


def test_production_setup_and_lan_dispatch_for_windows_and_macos(tmp_path):
    services = FakeServices()
    linux_only_caps = {"xclip", "portal_screenshot", "portal_shortcuts", "maim", "evdev_keyboards", "hotkey_conflicts"}

    # 1. Windows 真实组合链：/api/setup/check 走 WindowsPlatformAdapter，/api/v1/lan-access 走 ipconfig（绝不调 ip）
    win_cmds: list[list[str]] = []

    def win_run(cmd, **_kw):
        win_cmds.append(list(cmd))
        assert cmd == ["ipconfig"]
        out = (
            "无线局域网适配器 WLAN:\n"
            "   IPv4 地址 . . . . . . . . . . . . : 192.168.1.42(首选)\n"
            "以太网适配器 vEthernet (WSL):\n"
            "   IPv4 地址 . . . . . . . . . . . . : 172.24.16.1(首选)\n"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    win_paths = AppPaths.for_roots(
        config_root=tmp_path / "win" / "c",
        data_root=tmp_path / "win" / "d",
        runtime_dir=tmp_path / "win" / "r",
        os_name="windows",
        environ={"APPDATA": str(tmp_path / "live-app"), "LOCALAPPDATA": str(tmp_path / "live-local")},
    )
    win_adapters = SystemAdapters(
        environ={},
        run=win_run,
        which=lambda _n: None,
        ocr_languages=lambda: ["chi_sim", "chi_tra", "eng"],
        http_transport=httpx.MockTransport(services),
    )
    win_rt = compose_production_runtime(paths=win_paths, token=FAKE_TOKEN, adapters=win_adapters, endpoints=ENDPOINTS)
    with TestClient(create_app(win_rt, static_v3=STATIC_V3), client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945") as c:
        chk = c.get("/api/setup/check").json()
        assert chk["session_type"] == "windows"
        cap_ids = {cap["id"] for cap in chk["capabilities"]}
        assert {"win32_input", "win32_capture", "win32_hotkeys", "ocr_chi_sim", "ocr_chi_tra", "ocr_eng"} <= cap_ids
        assert cap_ids.isdisjoint(linux_only_caps)

        lan = c.get("/api/v1/lan-access")
        assert lan.status_code == 200
        assert [e["ip"] for e in lan.json()["entries"]] == ["192.168.1.42"]
        assert win_cmds == [["ipconfig"]]

    # 2. macOS 真实组合链：/api/setup/check 走 MacOSPlatformAdapter，/api/v1/lan-access 走 ifconfig（绝不调 ip）
    mac_cmds: list[list[str]] = []

    def mac_run(cmd, **_kw):
        mac_cmds.append(list(cmd))
        assert cmd == ["ifconfig"]
        out = (
            "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tinet 192.168.50.7 netmask 0xffffff00 broadcast 192.168.50.255\n"
            "utun2: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n"
            "\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff\n"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    mac_paths = AppPaths.for_roots(
        config_root=tmp_path / "mac" / "c",
        data_root=tmp_path / "mac" / "d",
        runtime_dir=tmp_path / "mac" / "r",
        os_name="macos",
        environ={"HOME": str(tmp_path / "live-home")},
    )
    mac_adapters = SystemAdapters(
        environ={},
        run=mac_run,
        which=lambda _n: None,
        ocr_languages=lambda: ["chi_sim", "chi_tra", "eng"],
        http_transport=httpx.MockTransport(services),
    )
    mac_rt = compose_production_runtime(paths=mac_paths, token=FAKE_TOKEN, adapters=mac_adapters, endpoints=ENDPOINTS)
    with TestClient(create_app(mac_rt, static_v3=STATIC_V3), client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945") as c:
        chk = c.get("/api/setup/check").json()
        assert chk["session_type"] == "macos"
        cap_ids = {cap["id"] for cap in chk["capabilities"]}
        assert {"macos_accessibility", "macos_screen_recording", "clipboard", "ocr_chi_sim", "ocr_chi_tra", "ocr_eng"} <= cap_ids
        assert cap_ids.isdisjoint(linux_only_caps)

        lan = c.get("/api/v1/lan-access")
        assert lan.status_code == 200
        assert [e["ip"] for e in lan.json()["entries"]] == ["192.168.50.7"]
        assert mac_cmds == [["ifconfig"]]


def test_setup_worker_real_loopback_ollama_pull_fakeip_chain(tmp_path: Path):
    """真链路（无 MockTransport）：真实 loopback HTTPServer + OVI_PORT_OLLAMA + 真实 setup_worker 子进程。"""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from _v3.production import build_production_app

    presigned = (
        "https://dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com/blobs/sha256/abc?"
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=" + ("f" * 500)
    )
    fakeip_err = (
        f'max retries exceeded: Get "{presigned}": redirect target not allowed: '
        'host "dd20bb.r2.cloudflarestorage.com" resolves to non-public 198.18.75.191'
    )
    state: dict[str, object] = {"mode": "recover", "models": [], "pulls": []}

    class OllamaLoopbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/version":
                payload = {"version": "0.40.16"}
            elif self.path == "/api/tags":
                payload = {"models": [{"name": m, "model": m, "size": 3_300_000_000} for m in state["models"]]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != "/api/pull":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("content-length") or "0")
            body = json.loads(self.rfile.read(length) or b"{}")
            pulls = state["pulls"]
            assert isinstance(pulls, list)
            pulls.append(body)
            if not body.get("insecure"):
                lines = [
                    {"status": "pulling manifest"},
                    {"error": fakeip_err},
                ]
            elif state["mode"] == "recover":
                models = state["models"]
                assert isinstance(models, list)
                models.append(body["model"])
                lines = [
                    {"status": "pulling layer", "digest": "sha256:l1", "total": 100, "completed": 100},
                    {"status": "success"},
                ]
            else:
                lines = [{"error": f'max retries exceeded: Get "{presigned}": context deadline exceeded'}]
            raw = ("\n".join(json.dumps(item) for item in lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return None

    def _run_install_all(root: Path, ollama_port: int) -> tuple[dict, Path]:
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "OVI_"))},
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "OVI_PORT_OLLAMA": str(ollama_port),
            "PYTHONPATH": str(PROGRAM_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        app, paths = build_production_app(environ=env)
        # 仅演练第 3 步 ollama_pull：预置已安装的二进制与 ASR 权重使其他步骤跳过
        paths.ollama_binary.parent.mkdir(parents=True, exist_ok=True)
        paths.ollama_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        paths.ollama_binary.chmod(0o755)
        paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
        host_probe = probe_asr_hardware(paths.os_name, platform.machine(), environ=env)
        seeded_variant = host_probe.target_variant or "cpu"
        paths.asr_manifest.write_text(json.dumps({"variant": seeded_variant}), encoding="utf-8")
        paths.asr_model_dir.mkdir(parents=True, exist_ok=True)
        (paths.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
        (paths.asr_model_dir / "model.safetensors").write_bytes(b"w")

        with TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945") as client:
            report = {
                "components": [
                    {"name": n, "state": "running", "since": "2026-11-23T00:00:00+00:00"}
                    for n in ("ollama", "asr", "server")
                ]
            }
            assert client.post("/api/v1/components", json=report, headers=H).status_code == 202
            started = client.post("/api/setup/install-all", headers=H)
            assert started.status_code == 202
            deadline = time.monotonic() + 25.0
            check: dict = {}
            while time.monotonic() < deadline:
                check = client.get("/api/setup/check").json()
                if check["install_all"]["state"] in ("done", "failed"):
                    return check, paths
                time.sleep(0.1)
            pytest.fail(f"等待一键装齐终态超时：{check}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaLoopbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 路径 1：首次 Fake-IP 拦截 → 真实 setup_worker 经 OVI_PORT_OLLAMA 自动 insecure 重试成功
        ok_check, ok_paths = _run_install_all(tmp_path / "loopback-ok", port)
        assert ok_check["install_all"]["state"] == "done"
        assert ok_check["install_all"]["steps"][2]["state"] == "done"
        assert ok_check["ollama"]["model_status"] == "ready"
        assert state["pulls"] == [
            {"model": "qwen3.5:4b", "stream": True},
            {"model": "qwen3.5:4b", "stream": True, "insecure": True},
        ]
        ok_log = (ok_paths.logs / "setup-ollama_pull.log").read_text(encoding="utf-8")
        assert "X-Amz-Signature=" in ok_log and "198.18.75.191" in ok_log

        # 路径 2：insecure 重试仍超时 → UI 返回简短中文与实际 OVI_PORT_OLLAMA 手动指引，日志完整留痕
        state["mode"] = "fail_retry"
        state["models"] = []
        state["pulls"] = []
        fail_check, fail_paths = _run_install_all(tmp_path / "loopback-fail", port)
        assert fail_check["install_all"]["state"] == "failed"
        assert fail_check["install_all"]["steps"][2]["state"] == "failed"
        assert fail_check["ollama"]["model_status"] == "failed"
        err_msg = fail_check["ollama"]["message"]
        assert err_msg == fail_check["install_all"]["steps"][2]["message"]
        assert "Fake-IP" in err_msg and "超时" in err_msg
        assert f"OLLAMA_HOST=127.0.0.1:{port} ollama pull --insecure qwen3.5:4b" in err_msg
        assert "X-Amz-" not in err_msg and "https://" not in err_msg
        fail_log = (fail_paths.logs / "setup-ollama_pull.log").read_text(encoding="utf-8")
        assert "attempt=1" in fail_log and "attempt=2" in fail_log and "X-Amz-Signature=" in fail_log
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_asr_runtime_server_cleanup_is_noop_and_worker_cleanup_is_conditional(tmp_path: Path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "OVI_"))},
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    paths = _paths_for(tmp_path / "srv")
    paths.asr_venv.mkdir(parents=True)
    paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.asr_manifest.write_text(json.dumps({"variant": "cpu"}), encoding="utf-8")
    kinds = setup_task_kinds(paths, SystemAdapters(environ=env))
    kinds[SetupTaskKind.asr_runtime_install].cleanup()
    # 服务端 cleanup 绝不误删既有 cpu 环境
    assert paths.asr_venv.is_dir() and paths.asr_manifest.is_file()

    # worker 侧在进入替换阶段之前（on_replace_start 未触发）中止清理同样不动既有环境
    from _paths import default_app_paths

    live_paths = default_app_paths(env)
    live_paths.asr_venv.mkdir(parents=True)
    live_paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
    live_paths.asr_manifest.write_text(json.dumps({"variant": "cpu"}), encoding="utf-8")
    _action, worker_cleanup, cancel_event = setup_worker_kind(SetupTaskKind.asr_runtime_install, env)
    assert not cancel_event.is_set()
    worker_cleanup()
    assert live_paths.asr_venv.is_dir() and live_paths.asr_manifest.is_file()


def test_setup_task_actions_wires_proxy_release_client_download_env_and_runtime_popen(tmp_path: Path, monkeypatch):
    def _win_paths(name: str) -> AppPaths:
        return AppPaths.for_roots(
            config_root=tmp_path / name / "c",
            data_root=tmp_path / name / "d",
            runtime_dir=tmp_path / name / "r",
            os_name="windows",
            environ={"APPDATA": str(tmp_path / name / "a"), "LOCALAPPDATA": str(tmp_path / name / "l")},
        )

    class _Captured(Exception):
        pass

    captured_clients: list[httpx.Client] = []
    captured_pull: list[httpx.Client] = []

    def _capture_download(client: httpx.Client, **_kw):
        captured_clients.append(client)
        raise _Captured()

    def _capture_pull(client: httpx.Client, _payload, _report):
        captured_pull.append(client)
        raise _Captured()

    monkeypatch.setattr(setup_actions_mod, "download_verified", _capture_download)
    monkeypatch.setattr(setup_actions_mod, "_pull_stream_once", _capture_pull)

    # 1. release_client 延迟构造：SOCKS5 代理在 setup_task_actions() 构造期不报错，执行 ollama_install 时才抛明确错且不泄密
    socks_paths = _win_paths("socks")
    socks_paths.config_root.mkdir(parents=True)
    socks_adapters = SystemAdapters(
        environ={"HTTPS_PROXY": "socks5://alice:s3cr3t@127.0.0.1:1080"},
        http_transport=None,
        read_system_proxy=lambda _os: ({}, []),
    )
    socks_actions = setup_task_actions(socks_paths, ENDPOINTS, socks_adapters, ConfigStore(socks_paths.config))
    with pytest.raises(LogicError) as exc_info:
        socks_actions[SetupTaskKind.ollama_install](lambda _p: None)
    assert exc_info.value.code == ErrorCode.dependency_failed
    assert "SOCKS" in exc_info.value.message
    assert "s3cr3t" not in exc_info.value.message

    # 2. release_client 读系统代理 + 有限 read=120s；ollama_pull 保持独立 long_read(read=None)；注入 transport 透传无 mounts
    sys_proxy = (
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7891"},
        ["*.corp"],
    )

    win_paths = _win_paths("wired")
    win_paths.config_root.mkdir(parents=True)
    real_transport_adapters = SystemAdapters(
        environ={},
        http_transport=None,
        read_system_proxy=lambda os_name: sys_proxy if os_name == "windows" else ({}, []),
    )
    actions = setup_task_actions(win_paths, ENDPOINTS, real_transport_adapters, ConfigStore(win_paths.config))
    with pytest.raises(_Captured):
        actions[SetupTaskKind.ollama_install](lambda _p: None)
    rel_client = captured_clients[-1]
    assert rel_client.timeout.read == 120.0
    assert rel_client._trust_env is False
    assert rel_client._transport_for_url(httpx.URL("https://github.com/ollama")) is not rel_client._transport
    assert rel_client._transport_for_url(httpx.URL("http://127.0.0.1:17945")) is rel_client._transport
    assert rel_client._transport_for_url(httpx.URL("https://a.corp")) is rel_client._transport

    with pytest.raises(_Captured):
        actions[SetupTaskKind.ollama_pull](lambda _p: None)
    assert captured_pull[-1].timeout.read is None

    mock_transport = httpx.MockTransport(FakeServices())
    mock_adapters = SystemAdapters(
        environ={},
        http_transport=mock_transport,
        read_system_proxy=lambda _os: sys_proxy,
    )
    mock_actions = setup_task_actions(win_paths, ENDPOINTS, mock_adapters, ConfigStore(win_paths.config))
    with pytest.raises(_Captured):
        mock_actions[SetupTaskKind.ollama_install](lambda _p: None)
    assert captured_clients[-1]._transport is mock_transport
    assert captured_clients[-1]._mounts == {}

    # 3. Step 5 download_env 合并系统代理并大小写去重 HF_HOME / HF_HUB_DISABLE_PROGRESS_BARS / PYTHONUTF8
    download_calls: list[dict[str, str]] = []

    class _DoneProc:
        pid = 43210
        returncode = 0

        def __init__(self) -> None:
            self.stdout = io.StringIO("")

        def poll(self):
            return 0

    def _capture_download_popen(_argv, **kwargs):
        download_calls.append(dict(kwargs["env"]))
        return _DoneProc()

    win_paths.asr_python.parent.mkdir(parents=True, exist_ok=True)
    win_paths.asr_python.write_text("", encoding="utf-8")
    win_paths.asr_model_dir.mkdir(parents=True, exist_ok=True)
    (win_paths.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (win_paths.asr_model_dir / "model.safetensors").write_bytes(b"w")

    dl_adapters = SystemAdapters(
        environ={"hf_home": "C:\\old-hf", "hf_hub_disable_progress_bars": "0", "pythonutf8": "0"},
        popen=_capture_download_popen,
        http_transport=mock_transport,
        read_system_proxy=lambda os_name: sys_proxy if os_name == "windows" else ({}, []),
    )
    dl_actions = setup_task_actions(win_paths, ENDPOINTS, dl_adapters, ConfigStore(win_paths.config))
    assert dl_actions[SetupTaskKind.asr_download](lambda _p: None).endswith("已下载")
    dl_env = download_calls[-1]
    assert dl_env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert dl_env["HTTPS_PROXY"] == "http://127.0.0.1:7891"
    assert "*.corp" in dl_env["NO_PROXY"]
    assert dl_env["HF_HOME"] == str(win_paths.hf_home) and "hf_home" not in dl_env
    assert dl_env["HF_HUB_DISABLE_PROGRESS_BARS"] == "1" and "hf_hub_disable_progress_bars" not in dl_env
    assert dl_env["PYTHONUTF8"] == "1" and "pythonutf8" not in dl_env

    # 4. Step 4 asr_runtime_install 透传 adapters.popen 与 adapters.read_system_proxy
    rt_paths = _win_paths("runtime")
    rt_paths.config_root.mkdir(parents=True)
    rt_paths.uv_dir.mkdir(parents=True, exist_ok=True)
    (rt_paths.uv_dir / "uv.exe").write_text("", encoding="utf-8")
    uv_calls: list[tuple[list[str], dict[str, str]]] = []

    def _capture_uv_popen(argv, **kwargs):
        uv_calls.append((list(argv), dict(kwargs["env"])))
        return _DoneProc()

    rt_adapters = SystemAdapters(
        environ={},
        popen=_capture_uv_popen,
        which=lambda _name: None,
        http_transport=mock_transport,
        read_system_proxy=lambda os_name: sys_proxy if os_name == "windows" else ({}, []),
    )
    rt_actions = setup_task_actions(rt_paths, ENDPOINTS, rt_adapters, ConfigStore(rt_paths.config))
    res = rt_actions[SetupTaskKind.asr_runtime_install](lambda _p: None)
    assert "cpu" in res
    assert len(uv_calls) == 4
    for argv, env in uv_calls:
        assert "--color" in argv and "never" in argv
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7891"
        assert env["UV_SYSTEM_CERTS"] == "1"
        assert env["UV_CONCURRENT_DOWNLOADS"] == "8"
        assert env["UV_HTTP_TIMEOUT"] == "120"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 桩可执行脚本真链验收")
def test_setup_worker_real_asr_runtime_streaming_log_progress_and_host_exit(tmp_path: Path):
    """真链验收：真实 setup_worker 子进程 + 本地桩 uv 脚本（显式 CUDA_VISIBLE_DEVICES="" 保 CPU 变体确定）。

    路径 1：POST /api/setup/install-all → GET /api/setup/check 实时呈现 percent=None、total_bytes=None、
    completed_bytes 递增与包名进度，同时 setup-asr_runtime_install.log 在运行中实时落盘并脱敏代理凭据，最终完成。
    路径 2：宿主进程退出 → setup_worker 中止任务并终止 uv 及后代子进程，不写 manifest。
    """
    strip_exact = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}

    def _isolated_env(root: Path) -> dict[str, str]:
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("XDG_", "OVI_", "UV_")) and k.lower() not in strip_exact
        }
        return {
            **base,
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "PYTHONPATH": str(PROGRAM_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "HTTPS_PROXY": "http://alice:topsecret@127.0.0.1:7890",
        }

    def _write_uv_stub(
        uv_path: Path,
        *,
        gate_file: Path,
        env_dump: Path | None = None,
        pids_file: Path | None = None,
    ) -> None:
        uv_path.parent.mkdir(parents=True, exist_ok=True)
        script = f"""#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path

args = sys.argv[1:]
sub = args[0] if args else ""
gate = Path({str(gate_file)!r})
env_dump = {(str(env_dump) if env_dump is not None else None)!r}
pids_file = {(str(pids_file) if pids_file is not None else None)!r}

if sub == "python":
    if env_dump:
        keys = ("HTTPS_PROXY", "UV_SYSTEM_CERTS", "UV_CONCURRENT_DOWNLOADS", "UV_HTTP_TIMEOUT", "NO_COLOR", "PYTHONUTF8")
        Path(env_dump).write_text(json.dumps({{k: os.environ.get(k) for k in keys}}), encoding="utf-8")
    print("Downloading cpython-3.12.13-linux-x86_64-none (download) (21.5MiB)", flush=True)
    raise SystemExit(0)

if sub == "venv":
    Path(args[-1]).mkdir(parents=True, exist_ok=True)
    raise SystemExit(0)

if sub == "pip":
    cache_dir = Path(os.environ["UV_CACHE_DIR"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "torch.tmp").write_bytes(b"x" * (256 * 1024))
    print("Resolved 62 packages in 0.1s", flush=True)
    print("Downloading torch (2.7GiB)", flush=True)
    print("fetch http://alice:topsecret@127.0.0.1:7890/torch.whl", flush=True)
    child = None
    if pids_file:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        tmp = Path(pids_file + ".tmp")
        tmp.write_text(json.dumps({{"uv": os.getpid(), "child": child.pid}}), encoding="utf-8")
        os.replace(tmp, pids_file)
    deadline = time.monotonic() + 30.0
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if child is not None:
        child.terminate()
    print("Installed 62 packages in 0.2s", flush=True)
    raise SystemExit(0)

if sub == "cache":
    raise SystemExit(0)

raise SystemExit(2)
"""
        uv_path.write_text(script, encoding="utf-8")
        uv_path.chmod(0o755)

    def _wait_until(predicate, timeout_s: float = 20.0):
        deadline = time.monotonic() + timeout_s
        while True:
            val = predicate()
            if val:
                return val
            assert time.monotonic() < deadline, "等待真链状态超时"
            time.sleep(0.1)

    # 路径 1：真实 API + 真实 setup_worker 子进程实时进度、实时脱敏日志与最终落盘
    root1 = tmp_path / "real-chain-ok"
    env1 = _isolated_env(root1)
    paths1 = default_app_paths(env1)
    gate1 = root1 / "release-pip"
    env_dump1 = root1 / "uv-env.json"
    _write_uv_stub(paths1.uv_dir / "uv", gate_file=gate1, env_dump=env_dump1)
    # 预置其他步骤为已就绪，确保一键装齐直达 Step 4 asr_runtime_install
    paths1.ollama_binary.parent.mkdir(parents=True, exist_ok=True)
    paths1.ollama_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    paths1.ollama_binary.chmod(0o755)
    paths1.asr_model_dir.mkdir(parents=True, exist_ok=True)
    (paths1.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (paths1.asr_model_dir / "model.safetensors").write_bytes(b"w")

    services = FakeServices()
    services.models = ["qwen3.5:4b"]
    adapters1 = SystemAdapters(environ=env1, http_transport=httpx.MockTransport(services))
    runtime1 = compose_production_runtime(paths=paths1, token=FAKE_TOKEN, adapters=adapters1, endpoints=ENDPOINTS)
    log1 = paths1.logs / "setup-asr_runtime_install.log"
    try:
        with TestClient(
            create_app(runtime1, static_v3=STATIC_V3),
            client=("127.0.0.1", 50000),
            base_url="https://127.0.0.1:17945",
        ) as client:
            report = {
                "components": [
                    {"name": n, "state": "running", "since": "2026-11-23T00:00:00+00:00"}
                    for n in ("ollama", "asr", "server")
                ]
            }
            assert client.post("/api/v1/components", json=report, headers=H).status_code == 202
            assert client.post("/api/setup/install-all", headers=H).status_code == 202

            def _step4_live():
                chk = client.get("/api/setup/check").json()
                step4 = chk["install_all"]["steps"][3]
                prog = step4.get("progress") or {}
                if (
                    step4["state"] == "running"
                    and (prog.get("completed_bytes") or 0) >= 256 * 1024
                    and "torch (2.7GiB)" in (prog.get("stage") or "")
                ):
                    return step4
                return None

            live_step4 = _wait_until(_step4_live)
            prog = live_step4["progress"]
            assert prog["percent"] is None and prog["total_bytes"] is None
            assert prog["completed_bytes"] >= 256 * 1024
            assert "torch (2.7GiB)" in live_step4["message"]
            # 运行中日志已实时落盘且不含代理密码
            live_log = log1.read_text(encoding="utf-8")
            assert "Downloading torch (2.7GiB)" in live_log
            assert "http://***@127.0.0.1:7890/torch.whl" in live_log
            assert "topsecret" not in live_log

            gate1.write_text("go", encoding="utf-8")
            done_chk = _wait_until(
                lambda: (c := client.get("/api/setup/check").json())
                and c["install_all"]["state"] in ("done", "failed")
                and c
            )
            assert done_chk["install_all"]["state"] == "done"
            assert done_chk["install_all"]["steps"][3]["state"] == "done"
            assert paths1.asr_manifest.is_file()
            assert json.loads(paths1.asr_manifest.read_text(encoding="utf-8"))["variant"] == "cpu"
            seen_env = json.loads(env_dump1.read_text(encoding="utf-8"))
            assert seen_env == {
                "HTTPS_PROXY": "http://alice:topsecret@127.0.0.1:7890",
                "UV_SYSTEM_CERTS": "1",
                "UV_CONCURRENT_DOWNLOADS": "8",
                "UV_HTTP_TIMEOUT": "120",
                "NO_COLOR": "1",
                "PYTHONUTF8": "1",
            }
    finally:
        gate1.write_text("cleanup", encoding="utf-8")

    # 路径 2：真实宿主退出 → setup_worker 中止任务并终止 uv 与其后代进程，绝不写 manifest
    root2 = tmp_path / "real-chain-host-exit"
    env2 = _isolated_env(root2)
    paths2 = default_app_paths(env2)
    gate2 = root2 / "never-release"
    pids_file = root2 / "pids.json"
    _write_uv_stub(paths2.uv_dir / "uv", gate_file=gate2, pids_file=pids_file)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pids: dict[str, int] = {}
    try:
        mgr = SetupTaskManager(
            kinds=setup_task_kinds(paths2, SystemAdapters(environ=env2)),
            tasks_dir=paths2.setup_tasks,
            log_dir=paths2.logs,
            worker_argv=[sys.executable, "-m", "_v3.setup_worker"],
            owner_pid=host.pid,
            environ=env2,
        )
        mgr.start(SetupTaskKind.asr_runtime_install)
        _wait_until(
            lambda: pids_file.is_file()
            and (t := mgr.latest(SetupTaskKind.asr_runtime_install)) is not None
            and t.progress is not None
            and "torch (2.7GiB)" in (t.progress.stage or "")
        )
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        host.kill()
        host.wait(timeout=5)
        done = _wait_until(
            lambda: (t := mgr.latest(SetupTaskKind.asr_runtime_install)) is not None
            and t.state != SetupTaskState.running
            and t
        )
        assert done.state == SetupTaskState.interrupted
        assert done.message == APP_EXIT_MESSAGE
        _wait_until(
            lambda: process_identity(pids["uv"]) is None and process_identity(pids["child"]) is None,
            timeout_s=5.0,
        )
        assert not paths2.asr_manifest.exists()
    finally:
        gate2.write_text("cleanup", encoding="utf-8")
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)
        for pid in pids.values():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_production_logs_about_shared_hardware_cache_and_step5_log_unification(world):
    client = world["client"]
    paths = world["paths"]
    paths.logs.mkdir(parents=True, exist_ok=True)
    (paths.logs / "app.log").write_text(
        f"2026-11-23 [INFO] started with {FAKE_TOKEN}\n"
        f"2026-11-23 [WARN] proxy http://alice:secret@127.0.0.1:7890\n",
        encoding="utf-8",
    )

    # 1) /api/v1/about 与 /api/setup/check 共用唯一 30s CachedHardwareProbe（nvidia-smi 仅调用一次）
    nvidia_before = sum(1 for c in world["run"].calls if Path(c[0]).name.lower().startswith("nvidia-smi"))
    about_resp = client.get("/api/v1/about")
    assert about_resp.status_code == 200, about_resp.text
    about = about_resp.json()
    assert about["ok"] is True and about["session_type"] == "wayland"
    assert about["logs_dir"] == str(paths.logs) and about["data_root"] == str(paths.data_root)
    assert client.get("/api/setup/check").status_code == 200
    nvidia_after = sum(1 for c in world["run"].calls if Path(c[0]).name.lower().startswith("nvidia-smi"))
    assert nvidia_after - nvidia_before == 1

    # 2) 生产 LogService 脱敏 FAKE_TOKEN 与代理凭证
    read = client.get("/api/v1/logs/app.log").json()
    text = "\n".join(e["text"] for e in read["entries"])
    assert FAKE_TOKEN not in text and "alice:secret" not in text
    summary = client.get("/api/v1/logs/diagnostic-summary").json()
    assert summary["ok"] is True and FAKE_TOKEN not in summary["text"]

    # 3) Step 5 asr_download 统一走 stderr=subprocess.PIPE，绝不再建旧 setup-asr-download.log
    paths.asr_python.parent.mkdir(parents=True, exist_ok=True)
    paths.asr_python.write_text("", encoding="utf-8")
    paths.asr_model_dir.mkdir(parents=True, exist_ok=True)
    (paths.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (paths.asr_model_dir / "model.safetensors").write_bytes(b"w")
    seen_stderr: list[object] = []

    class _Step5Proc:
        returncode = 0
        stdout = io.StringIO("")
        stderr = io.StringIO("step5 stderr line\n")

        def poll(self):
            return 0

    def spy_popen(_argv, **kw):
        seen_stderr.append(kw.get("stderr"))
        return _Step5Proc()

    step5_adapters = SystemAdapters(environ=world["adapters"].environ, popen=spy_popen, http_transport=world["adapters"].http_transport)
    actions = setup_task_actions(paths, ENDPOINTS, step5_adapters, world["runtime"].config)
    assert actions[SetupTaskKind.asr_download](lambda _p: None).endswith("已下载")
    assert seen_stderr == [subprocess.PIPE]
    assert not (paths.logs / "setup-asr-download.log").exists()

    # 4) 生产 SetupTaskManager 按 config.json 的 log_max_lines 启动前裁剪 setup-*.log
    assert client.post("/api/v1/config", json={"log_max_lines": 500}, headers=H).status_code == 200
    pull_log = paths.logs / "setup-ollama_pull.log"
    pull_log.write_text("".join(f"line-{i}\n" for i in range(600)), encoding="utf-8")
    world["runtime"].setup.tasks.start(SetupTaskKind.ollama_pull)
    assert len(pull_log.read_text(encoding="utf-8").splitlines()) == 500
