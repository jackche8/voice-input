"""生产工厂隔离装配：同一 compose_production_runtime + create_app(static/v3)，

隔离根目录 + 假令牌 + httpx.MockTransport（假 Ollama / 假 sidecar 主机名，不开任何套接字）+ 假进程。
验证：启动恢复、配置迁移、真实适配器接线、设置作业、局域网签发、激活门闩。不是官方模型或真机验收。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from _asr_provider import SidecarStreamProvider
from _contracts import (
    AudioInputDevice,
    AudioInputListResponse,
    OcrTextBlock,
    OcrTextLine,
    SetupTaskKind,
)
from _paths import INTERNAL_SETUP_WORKER_FLAG, PROGRAM_ROOT, STATIC_V3, AppPaths
from _v3.production import (
    ProductionEndpoints,
    SystemAdapters,
    compose_production_runtime,
    setup_task_actions,
    setup_task_kinds,
    setup_worker_kind,
)
from _v3.runtime_install import probe_asr_hardware
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
