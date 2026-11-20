"""§8.2-9 生产入口快照：薄 app.py / daemon.py、依赖清单与静态页。

不调用 systemctl、不启动服务、不碰在用的服务端口。systemd 单元、启动脚本与证书脚本已在 v3.4 阶段一删除（入口即应用本身）。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from _paths import PROGRAM_ROOT
from _v3.production import build_production_app

ROOT = PROGRAM_ROOT
LEGACY_MARKERS = ("faster_whisper", "deep_translator", "web_input", "/api/transcribe", "whisper_model", "WhisperModel")


def test_requirements_drop_whisper_keep_av():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "faster-whisper" not in req and "deep-translator" not in req
    assert "av==18.0.0" in req


def test_old_static_pages_removed_only_v3_served():
    for name in ("setup.html", "voice.html", "history.html", "api.html", "voice_setup.html"):
        assert not (ROOT / "static" / name).exists(), name
    assert (ROOT / "static" / "v3" / "index.html").is_file()
    assert (ROOT / "static" / "v3" / "voice.html").is_file()
    assert not (ROOT / "static" / "v3" / "setup.html").exists()


def _defs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]


def test_app_entry_is_thin_and_side_effect_free():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert _defs(ROOT / "app.py") == ["create_production_app"]
    for marker in LEGACY_MARKERS:
        assert marker not in src
    mod = importlib.import_module("app")
    assert not hasattr(mod, "app"), "导入 app 不得装配 live 应用"


def test_daemon_entry_is_thin():
    path = ROOT / "daemon.py"
    src = path.read_text(encoding="utf-8")
    assert _defs(path) == ["load_bindings", "main"]
    assert len(src.splitlines()) < 220
    # 顶层严禁直接导入 _linux.*（跨平台入口只按 os_name 延迟导入 Linux 专属通知）
    tree = ast.parse(src, filename="daemon.py")
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("_linux") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("_linux")
    for marker in LEGACY_MARKERS + ("/api/web_input", "TranslationOverlay", "StatusBar(", "import gi", "WebKit2", "sd_notify",
                                    "open_url_async", "= acquire_singleton_lock("):
        assert marker not in src
    for needed in (
        "_desktop.",
        "_platform",
        "_ui_desktop",
        "create_supervisor(",
        "MainWindow(",
        "SingleInstanceServer(",
        "run_as_second_instance(paths)",
        "on_retry_server=lambda: supervisor.retry(\"server\")",
        "on_config_changed=sync_autostart",
        "ensure_portal_app_entry(paths, os.environ)",
        "supervisor.stop(",
        "CaptureFlow",
        "DeliveryLoop",
        "VoiceHudOverlay",
        "StudioTrayIcon",
        "hotkey_rig.reset_voice()",
        "event_loop.request_stop()",
        # 快捷键组件失败经现有 error 事件进底栏，事件流每次连上重发（main() 需要真实 Qt/平台/单例锁，测试里跑不了，只能扫接线）
        "on_error=status.set_hotkey_error",
        "on_connected=status.resend_on_connect",
    ):
        assert needed in src
    assert src.index("event_loop.request_stop()") < src.index("api.close()") < src.index("event_loop.stop(")


def test_build_production_app_binds_environ_roots_and_serves_pages(tmp_path):
    env = {
        "XDG_SESSION_TYPE": "x11",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    app, paths = build_production_app(environ=env)
    assert paths.live and paths.data_root == tmp_path / "data" / "ollama-voice-input"
    assert paths.token.is_file() and (paths.token.stat().st_mode & 0o777) == 0o600
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="https://127.0.0.1:17945")
    body = client.get("/api/v1/status", headers={"Origin": "https://127.0.0.1:17945"}).json()
    assert body["ok"] is True and set(body) == {"ok", "service", "injected"}
    assert client.get("/setup").status_code == 200
    for old in ("/api/transcribe", "/api/web_input/poll", "/voice_setup", "/api_docs"):
        assert client.get(old).status_code in (403, 404, 405), old
    client.close()
