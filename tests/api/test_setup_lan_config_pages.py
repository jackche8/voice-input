"""设置真实状态/作业、局域网签发、配置视图、唯一界面入口与旧路径删除。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _contracts import ComponentReport, ComponentStatus, SetupTaskKind
from _v3.component_routes import ComponentRegistry
from _v3.production import STATIC_V3
from _v3_app import build_sandbox_runtime, create_app, export_openapi_document
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.setup_fixtures import FAKE_LAN_IPS, FakeWorld, build_setup, task

LEGACY_PATHS = (
    ("POST", "/api/transcribe"),
    ("POST", "/api/ocr_translate"),
    ("GET", "/api/web_input/config"),
    ("GET", "/api/web_input/info"),
    ("POST", "/api/web_input/submit"),
    ("GET", "/api/web_input/poll"),
    ("POST", "/api/web_input/report"),
    ("GET", "/api/web_input/history"),
    ("POST", "/api/web_input/resend"),
    ("POST", "/api/setup/pull_model"),
    ("POST", "/api/setup/delete_model"),
    ("POST", "/api/setup/install_ollama"),
    ("POST", "/api/setup/start_ollama"),
    ("POST", "/api/setup/preload_whisper"),
    ("POST", "/api/setup/delete_whisper"),
    ("POST", "/api/setup/set_config"),
    ("GET", "/history"),
    ("GET", "/voice_setup"),
    ("GET", "/api_docs"),
    ("GET", "/static/setup.html"),
    ("GET", "/static/voice.html"),
    ("GET", "/static/v3/setup.html"),
)


def _app(tmp_path: Path, *, world: FakeWorld | None = None, tasks=None, components=None, lan=True, static=True):
    runtime = build_sandbox_runtime(
        root=tmp_path / "sb",
        token=TEST_TOKEN,
        setup=None if world is None else build_setup(world, tasks, components=components),
        lan_ips=(lambda: list(FAKE_LAN_IPS)) if lan else None,
    )
    return runtime, create_app(runtime, static_v3=STATIC_V3 if static else None)


# ---------------------------------------------------------------- setup/check


def test_setup_check_without_injection_is_visible_failure(tmp_path):
    _rt, app = _app(tmp_path)
    res = local_client(app).get("/api/setup/check")
    assert res.status_code == 503
    assert res.json()["ok"] is False


def test_setup_check_reports_missing_and_wayland_portal(tmp_path):
    world = FakeWorld()
    _rt, app = _app(tmp_path, world=world)
    body = local_client(app).get("/api/setup/check").json()
    assert body["session_type"] == "wayland"
    assert body["asr"]["status"] == "missing"
    assert "config.json" in body["asr"]["message"]
    assert body["ollama"]["running"] is False and body["ollama"]["model_status"] == "missing"
    assert body["ollama"]["message"].startswith("Ollama 纠错/翻译服务：尚未收到应用的组件状态")
    assert body["components"] == []
    caps = {c["id"]: c for c in body["capabilities"]}
    assert caps["portal_screenshot"]["available"] and caps["portal_screenshot"]["required"]
    assert caps["portal_shortcuts"]["available"]
    assert "maim" not in caps
    assert caps["xclip"]["available"] and not caps["notify-send"]["available"]
    assert caps["ocr_chi_sim"]["available"] and not caps["ocr_jpn"]["available"] and not caps["ocr_jpn"]["required"]


def test_setup_check_x11_and_unknown_session(tmp_path):
    world = FakeWorld(session="x11", tools={"xclip": "/usr/bin/xclip", "maim": "/usr/bin/maim"})
    _rt, app = _app(tmp_path, world=world)
    caps = {c["id"]: c for c in local_client(app).get("/api/setup/check").json()["capabilities"]}
    assert caps["maim"]["available"] and caps["maim"]["required"]
    assert "portal_screenshot" not in caps
    world.session = "unknown"
    caps = {c["id"]: c for c in local_client(app).get("/api/setup/check").json()["capabilities"]}
    assert caps["session_type"]["available"] is False
    assert not caps["portal_screenshot"]["required"] and not caps["maim"]["required"]


def test_ollama_not_installed_then_not_running(tmp_path):
    from _v3.setup_probe import OllamaProbe

    world = FakeWorld(ollama=OllamaProbe(False, False, None, False, None, "Ollama 未响应: refused"))
    _rt, app = _app(tmp_path, world=world)
    client = local_client(app)
    body = client.get("/api/setup/check").json()["ollama"]
    assert body["installed"] is False and body["message"] == "未安装用户级 Ollama"
    world.ollama = OllamaProbe(True, False, None, False, None, "Ollama 未响应: refused")
    body = client.get("/api/setup/check").json()["ollama"]
    assert body["installed"] is True and body["running"] is False and "尚未收到应用的组件状态" in body["message"]


def test_check_shows_running_and_failed_task_state_never_fake(tmp_path):
    from _contracts import SetupProgress, SetupTaskState

    world = FakeWorld()
    running = task(SetupTaskState.running, "fixture 下载中", kind=SetupTaskKind.asr_download,
                   progress=SetupProgress(completed_bytes=50, total_bytes=200, percent=25.0, stage="fixture 下载中"))
    failed = task(SetupTaskState.failed, "网络被拒绝: fixture", kind=SetupTaskKind.ollama_pull)
    registry = ComponentRegistry()
    registry.replace(ComponentReport(components=[ComponentStatus(name=n, state="running", since="2026-11-20T00:00:00+00:00")
                                                 for n in ("ollama", "asr", "server")]))
    _rt, app = _app(tmp_path, world=world, tasks={SetupTaskKind.asr_download: running, SetupTaskKind.ollama_pull: failed},
                    components=registry)
    body = local_client(app).get("/api/setup/check").json()
    assert body["asr"]["status"] == "downloading" and body["asr"]["progress"]["percent"] == 25.0
    assert body["ollama"]["model_status"] == "failed" and "fixture" in body["ollama"]["message"]
    assert body["install_all"]["state"] == "idle"


def test_setup_routes_are_local_only(tmp_path):
    _rt, app = _app(tmp_path, world=FakeWorld())
    remote = lan_client(app)
    assert remote.get("/api/setup/check", headers=lan_headers()).status_code == 403
    assert remote.post("/api/setup/install-all", headers=lan_headers()).status_code == 403


# ---------------------------------------------------------------- LAN access


def test_lan_access_local_issues_entries_qr_and_token(tmp_path):
    _rt, app = _app(tmp_path)
    body = local_client(app).get("/api/v1/lan-access").json()
    assert body["port"] == 17945 and body["token"] == TEST_TOKEN
    assert TEST_TOKEN not in body["token_masked"]
    assert [e["ip"] for e in body["entries"]] == FAKE_LAN_IPS
    first = body["entries"][0]
    assert first["url"] == f"https://192.168.10.20:17945/voice#token={TEST_TOKEN}"
    assert TEST_TOKEN not in first["url_masked"]
    assert "#token=" in first["url_masked"]
    assert "<svg" in first["qr_svg"]


def test_lan_access_forbidden_remotely_even_with_token(tmp_path):
    _rt, app = _app(tmp_path)
    assert lan_client(app).get("/api/v1/lan-access", headers=lan_headers()).status_code == 403
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")
    assert public.get("/api/v1/lan-access", headers={"X-Voice-Token": TEST_TOKEN}).status_code == 403


def test_lan_access_without_provider_is_visible_failure(tmp_path):
    _rt, app = _app(tmp_path, lan=False)
    res = local_client(app).get("/api/v1/lan-access")
    assert res.status_code == 503 and res.json()["ok"] is False


def test_lan_from_audio_needs_token(tmp_path):
    _rt, app = _app(tmp_path)
    remote = lan_client(app)
    no_token = remote.post(
        "/api/v1/utterances/from-audio",
        data={"job_id": "j"},
        files={"audio": ("a.wav", b"x", "audio/wav")},
        headers=lan_headers(token=""),
    )
    assert no_token.status_code == 403


# ---------------------------------------------------------------- config view


def test_config_api_never_returns_legacy_secrets(tmp_path):
    runtime, app = _app(tmp_path)
    cfg_path = runtime.config.path
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data.update({
        "baidu_appid": "fixture-appid",
        "baidu_appkey": "fixture-secret",
        "whisper_model": "large-v3",
        "translate_target_lang": "en",
    })
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    client = local_client(app)
    got = client.get("/api/v1/config").json()
    assert "fixture-secret" not in json.dumps(got) and "baidu_appkey" not in got and "whisper_model" not in got
    assert "translate_target_lang" not in got
    assert got["translate_lang_pair"] == "zh-en" and got["translate_auto_enabled"] is True
    saved = client.post(
        "/api/v1/config",
        json={
            "custom_keywords": [" Alpha ", "", "Beta"],
            "hotkey_voice": "Control+Alt+Z",
            "polish_enabled": True,
            "translate_lang_pair": " ZH-EN ",
            "translate_auto_enabled": False,
        },
        headers=local_headers(),
    )
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["custom_keywords"] == ["Alpha", "Beta"] and out["hotkey_voice"] == "ctrl+alt+z"
    assert out["polish_enabled"] is True and "baidu_appkey" not in out
    assert out["translate_lang_pair"] == "zh-en" and out["translate_auto_enabled"] is False
    assert "translate_target_lang" not in out
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["baidu_appkey"] == "fixture-secret", "旧键在磁盘保留，不丢数据"
    assert disk["translate_target_lang"] == "en"
    assert disk["translate_lang_pair"] == "zh-en" and disk["translate_auto_enabled"] is False
    rejected = client.post("/api/v1/config", json={"baidu_appkey": "x"}, headers=local_headers())
    assert rejected.status_code == 400
    rejected_legacy = client.post("/api/v1/config", json={"translate_target_lang": "en"}, headers=local_headers())
    assert rejected_legacy.status_code == 400 and rejected_legacy.json()["error"] == "invalid_request"
    bad_pair = client.post("/api/v1/config", json={"translate_lang_pair": "fr-de"}, headers=local_headers())
    assert bad_pair.status_code == 400 and bad_pair.json()["error"] == "invalid_request"
    bad_key = client.post("/api/v1/config", json={"hotkey_voice": "z"}, headers=local_headers())
    assert bad_key.status_code == 400


# ---------------------------------------------------------------- pages


def test_pages_come_only_from_static_v3(tmp_path):
    _rt, app = _app(tmp_path)
    client = local_client(app)
    for url, name in (("/", "index.html"), ("/setup", "index.html"), ("/voice", "voice.html")):
        res = client.get(url)
        assert res.status_code == 200, url
        assert res.content == (STATIC_V3 / name).read_bytes()
        assert res.headers["referrer-policy"] == "no-referrer"
        assert b"Whisper" not in res.content and "百度".encode() not in res.content
    assert client.get("/").content == client.get("/setup").content
    for removed in ("/overlay-status", "/overlay-translate"):
        assert client.get(removed).status_code == 404, removed
    js_res = client.get("/js/business/http.js")
    assert js_res.status_code == 200 and js_res.headers.get("cache-control") == "no-cache"
    css_res = client.get("/css/theme.css")
    assert css_res.status_code == 200 and css_res.headers.get("cache-control") == "no-cache"
    assert css_res.headers.get("etag")
    assert client.get("/css/theme.css", headers={"If-None-Match": css_res.headers["etag"]}).status_code == 304
    snap = client.get("/openapi.snapshot.json")
    assert snap.status_code == 200 and snap.json()["paths"]


def test_phone_page_reachable_from_lan_but_setup_is_local(tmp_path):
    _rt, app = _app(tmp_path)
    remote = lan_client(app)
    lan_host = {"Host": "192.168.10.20:17945"}
    assert remote.get("/voice", headers=lan_host).status_code == 200
    assert remote.get("/js/voice.js", headers=lan_host).status_code == 200
    assert remote.get("/openapi.snapshot.json", headers=lan_host).status_code == 200
    assert remote.get("/", headers=lan_host).status_code == 403
    assert remote.get("/setup", headers=lan_host).status_code == 403
    assert remote.get("/openapi.json", headers=lan_host).status_code == 403
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")
    assert public.get("/voice", headers={"Host": "8.8.8.8:17945"}).status_code == 403


@pytest.mark.parametrize("method,path", LEGACY_PATHS)
def test_legacy_paths_are_gone_without_redirect(tmp_path, method, path):
    _rt, app = _app(tmp_path, world=FakeWorld())
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945", follow_redirects=False)
    res = client.request(method, path, headers=local_headers())
    assert res.status_code in {404, 405}, (path, res.status_code)
    assert "location" not in res.headers


def test_openapi_snapshot_is_the_generated_document(tmp_path):
    generated = export_openapi_document(root=tmp_path / "oa")
    snapshot = json.loads((STATIC_V3 / "openapi.json").read_text(encoding="utf-8"))
    assert snapshot == generated, "static/v3/openapi.json 必须由 scripts/gate.py --write-openapi-snapshot 生成"
    assert "/api/v1/lan-access" in snapshot["paths"]
    assert "/api/setup/install-all" in snapshot["paths"] and "/api/setup/tasks" not in snapshot["paths"]
    for schema in ("SetupCheckResponse", "InstallAllStatus", "LanAccessResponse", "AppConfigView", "AppConfigUpdate"):
        assert schema in snapshot["components"]["schemas"]
    assert TEST_TOKEN not in json.dumps(snapshot)
