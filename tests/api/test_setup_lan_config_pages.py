"""设置真实状态/作业、局域网签发、配置视图、唯一界面入口与旧路径删除。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _contracts import (
    AudioInputDevice,
    AudioInputListResponse,
    ComponentReport,
    ComponentStatus,
    ErrorCode,
    LogicError,
    SetupTaskKind,
)
from _paths import RESOURCES_DIR
from _v3.component_routes import ComponentRegistry
from _v3.pages import install_pages
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


def _app(
    tmp_path: Path,
    *,
    world: FakeWorld | None = None,
    tasks=None,
    components=None,
    lan=True,
    static=True,
    list_audio_inputs=None,
):
    runtime = build_sandbox_runtime(
        root=tmp_path / "sb",
        token=TEST_TOKEN,
        setup=None if world is None else build_setup(world, tasks, components=components),
        lan_ips=(lambda: list(FAKE_LAN_IPS)) if lan else None,
        list_audio_inputs=list_audio_inputs,
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
    assert got["ui_theme"] == "light" and got["audio_input_device"] == ""
    saved = client.post(
        "/api/v1/config",
        json={
            "custom_keywords": [" Alpha ", "", "Beta"],
            "hotkey_voice": "Control+Alt+Z",
            "polish_enabled": True,
            "translate_lang_pair": " ZH-EN ",
            "translate_auto_enabled": False,
            "ui_theme": "dark",
            "audio_input_device": "  USB Studio Mic  ",
        },
        headers=local_headers(),
    )
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["custom_keywords"] == ["Alpha", "Beta"] and out["hotkey_voice"] == "ctrl+alt+z"
    assert out["polish_enabled"] is True and "baidu_appkey" not in out
    assert out["translate_lang_pair"] == "zh-en" and out["translate_auto_enabled"] is False
    assert out["ui_theme"] == "dark" and out["audio_input_device"] == "USB Studio Mic"
    assert "translate_target_lang" not in out
    reset_mic = client.post("/api/v1/config", json={"audio_input_device": ""}, headers=local_headers())
    assert reset_mic.status_code == 200 and reset_mic.json()["audio_input_device"] == ""
    bad_theme = client.post("/api/v1/config", json={"ui_theme": "sepia"}, headers=local_headers())
    assert bad_theme.status_code == 400 and bad_theme.json()["error"] == "invalid_request"
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

    # log_max_lines：默认 2000，支持 500/2000/5000 往返持久化，非法值 400 invalid_request
    assert got["log_max_lines"] == 2000
    saved_cap = client.post("/api/v1/config", json={"log_max_lines": 5000}, headers=local_headers())
    assert saved_cap.status_code == 200 and saved_cap.json()["log_max_lines"] == 5000
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["log_max_lines"] == 5000
    bad_cap = client.post("/api/v1/config", json={"log_max_lines": 1234}, headers=local_headers())
    assert bad_cap.status_code == 400 and bad_cap.json()["error"] == "invalid_request"


# ---------------------------------------------------------------- audio inputs


def test_audio_inputs_route_mount_success_empty_and_single_track_503(tmp_path):
    # 1. 未注入 -> 单轨 HTTP 503 ErrorBody（绝不返 200+error）
    _rt_none, app_none = _app(tmp_path / "none")
    res_none = local_client(app_none).get("/api/v1/audio-inputs")
    assert res_none.status_code == 503
    body_none = res_none.json()
    assert body_none["ok"] is False and body_none["error"] == "dependency_failed"

    # 2. 正常返回多台设备 & 0 麦空列表 -> HTTP 200 AudioInputListResponse（无 error 字段）
    mode = {"state": "ok"}

    def _provider() -> AudioInputListResponse:
        if mode["state"] == "empty":
            return AudioInputListResponse(default_device=None, devices=[])
        if mode["state"] == "logic_err":
            raise LogicError(ErrorCode.dependency_failed, "PortAudio init failed")
        if mode["state"] == "raw_err":
            raise RuntimeError("sounddevice boom")
        return AudioInputListResponse(
            default_device="USB Mic",
            devices=[
                AudioInputDevice(name="USB Mic", hostapi="Windows DirectSound", is_default=True),
                AudioInputDevice(name="Headset", hostapi="Windows DirectSound", is_default=False),
            ],
        )

    _rt, app = _app(tmp_path / "ok", list_audio_inputs=_provider)
    client = local_client(app)
    res_ok = client.get("/api/v1/audio-inputs")
    assert res_ok.status_code == 200, res_ok.text
    payload = res_ok.json()
    assert payload == {
        "ok": True,
        "default_device": "USB Mic",
        "devices": [
            {"name": "USB Mic", "hostapi": "Windows DirectSound", "is_default": True},
            {"name": "Headset", "hostapi": "Windows DirectSound", "is_default": False},
        ],
    }
    assert "error" not in payload

    mode["state"] = "empty"
    res_empty = client.get("/api/v1/audio-inputs")
    assert res_empty.status_code == 200
    assert res_empty.json() == {"ok": True, "default_device": None, "devices": []}

    # 3. 底层抛 LogicError 或普通异常 -> 统一转 HTTP 503 dependency_failed
    for err_state, expected_text in (("logic_err", "PortAudio init failed"), ("raw_err", "sounddevice boom")):
        mode["state"] = err_state
        res_err = client.get("/api/v1/audio-inputs")
        assert res_err.status_code == 503, (err_state, res_err.text)
        err_body = res_err.json()
        assert err_body["ok"] is False and err_body["error"] == "dependency_failed"
        assert expected_text in err_body["message"]

    # 4. LAN 带合法 token 依然被 403 拒绝
    assert lan_client(app).get("/api/v1/audio-inputs", headers=lan_headers()).status_code == 403


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


def test_favicons_served_on_local_and_lan_forbidden_on_public_and_fail_fast(tmp_path):
    from fastapi import FastAPI

    _rt, app = _app(tmp_path)
    local = local_client(app)
    remote = lan_client(app)
    lan_host = {"Host": "192.168.10.20:17945"}
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")

    expected = (
        ("/favicon.svg", RESOURCES_DIR / "ollama-voice-input.svg", "image/svg+xml"),
        ("/favicon.ico", RESOURCES_DIR / "ollama-voice-input.ico", "image/x-icon"),
    )
    for url, disk_path, mime in expected:
        raw = disk_path.read_bytes()
        r_local = local.get(url)
        assert r_local.status_code == 200 and r_local.content == raw
        assert r_local.headers.get("cache-control") == "no-cache"
        assert mime in (r_local.headers.get("content-type") or "")

        r_lan = remote.get(url, headers=lan_host)
        assert r_lan.status_code == 200 and r_lan.content == raw

        r_pub = public.get(url, headers={"Host": "8.8.8.8:17945"})
        assert r_pub.status_code == 403

    empty_res = tmp_path / "empty-resources"
    empty_res.mkdir()
    with pytest.raises(RuntimeError, match="resources 缺少图标文件"):
        install_pages(FastAPI(), STATIC_V3, resources_dir=empty_res)


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
    assert "/api/v1/audio-inputs" in snapshot["paths"]
    assert "/api/setup/install-all" in snapshot["paths"] and "/api/setup/tasks" not in snapshot["paths"]
    for schema in (
        "SetupCheckResponse",
        "InstallAllStatus",
        "LanAccessResponse",
        "AppConfigView",
        "AppConfigUpdate",
        "AudioInputDevice",
        "AudioInputListResponse",
    ):
        assert schema in snapshot["components"]["schemas"]
    assert TEST_TOKEN not in json.dumps(snapshot)


def test_v3_app_lifespan_filters_only_proactor_connection_reset_10054_and_restores_handler(tmp_path):
    """B3：_v3_app lifespan 仅过滤源自 _ProactorBasePipeTransport._call_connection_lost 的 WinError 10054，其余全保留且退出条件还原。"""
    import asyncio
    from asyncio.proactor_events import _ProactorBasePipeTransport, _ProactorSocketTransport

    _rt, app = _app(tmp_path)

    def _make_reset(winerror: int | None) -> ConnectionResetError:
        exc = ConnectionResetError("connection reset")
        if winerror is not None:
            exc.winerror = winerror  # type: ignore[attr-defined]
        return exc

    class _SubTransport(_ProactorBasePipeTransport):
        def _call_connection_lost(self, exc):  # 子类覆写分支校验 isinstance(__self__, _ProactorBasePipeTransport)
            pass

    class _UnrelatedTransport:
        def _call_connection_lost(self, exc):
            pass

    def _fake_handle(cb):
        return type("_H", (), {"_callback": cb})()

    async def _run_cases() -> None:
        loop = asyncio.get_running_loop()
        forwarded: list[dict] = []

        def prev_handler(_loop, ctx):
            forwarded.append(ctx)

        loop.set_exception_handler(prev_handler)
        try:
            async with app.router.lifespan_context(app):
                active = loop.get_exception_handler()
                assert active is not None and active is not prev_handler

                # 1. 真实 _ProactorSocketTransport / 子类覆写的 _call_connection_lost + WinError 10054 → 精准吞掉
                sock_inst = object.__new__(_ProactorSocketTransport)
                sock_inst._sock = None
                sub_inst = object.__new__(_SubTransport)
                sub_inst._sock = None
                loop.call_exception_handler(
                    {"message": "cb", "exception": _make_reset(10054), "handle": _fake_handle(sock_inst._call_connection_lost)}
                )
                loop.call_exception_handler(
                    {"message": "cb", "exception": _make_reset(10054), "handle": _fake_handle(sub_inst._call_connection_lost)}
                )
                assert forwarded == []

                # 2. 其他 winerror / 无 winerror / 其他异常类型 / 非 Proactor 回调 / 缺 handle → 全部透传
                other_inst = _UnrelatedTransport()
                non_matching = [
                    {"message": "other_winerror", "exception": _make_reset(10053), "handle": _fake_handle(sock_inst._call_connection_lost)},
                    {"message": "no_winerror", "exception": _make_reset(None), "handle": _fake_handle(sock_inst._call_connection_lost)},
                    {"message": "runtime_err", "exception": RuntimeError("real bug"), "handle": _fake_handle(sock_inst._call_connection_lost)},
                    {"message": "unrelated_cb", "exception": _make_reset(10054), "handle": _fake_handle(other_inst._call_connection_lost)},
                    {"message": "plain_fn", "exception": _make_reset(10054), "handle": _fake_handle(lambda: None)},
                    {"message": "no_handle", "exception": _make_reset(10054)},
                ]
                for ctx in non_matching:
                    loop.call_exception_handler(ctx)
                assert [c["message"] for c in forwarded] == [c["message"] for c in non_matching]

            # 3. lifespan 退出后还原 prev_handler
            assert loop.get_exception_handler() is prev_handler

            # 4. 若外部在 lifespan 期间替换了自定义 handler，退出不得覆盖外部 handler
            custom_handler = lambda _l, _c: None  # noqa: E731
            async with app.router.lifespan_context(app):
                loop.set_exception_handler(custom_handler)
            assert loop.get_exception_handler() is custom_handler

            # 5. prev_handler 为 None 时，非命中异常走 loop.default_exception_handler
            loop.set_exception_handler(None)
            default_seen: list[dict] = []
            orig_default = loop.default_exception_handler
            loop.default_exception_handler = lambda ctx: default_seen.append(ctx)  # type: ignore[method-assign]
            try:
                async with app.router.lifespan_context(app):
                    loop.call_exception_handler({"message": "to_default", "exception": RuntimeError("keep")})
                assert len(default_seen) == 1 and default_seen[0]["message"] == "to_default"
                assert loop.get_exception_handler() is None
            finally:
                loop.default_exception_handler = orig_default  # type: ignore[method-assign]
        finally:
            loop.set_exception_handler(None)

    asyncio.run(_run_cases())
