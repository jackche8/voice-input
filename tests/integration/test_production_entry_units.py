"""§8.2-9 生产入口快照：薄 app.py / daemon.py、依赖清单与静态页。

不调用 systemctl、不启动服务、不碰在用的服务端口。systemd 单元、启动脚本与证书脚本已在 v3.4 阶段一删除（入口即应用本身）。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from _paths import (
    INTERNAL_CHILD_GUARD_FLAG,
    INTERNAL_SERVER_FLAG,
    INTERNAL_SETUP_WORKER_FLAG,
    INTERNAL_WINDOW_FLAG,
    PROGRAM_ROOT,
)
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
        "AnnotateOverlay",
        "open_annotator",
        "platform.write_clipboard_png",
        "pictures_dir(",
        "on_screenshot_annotate=actions.annotate",
        "on_annotate=actions.annotate",
        'attach_app_log(paths.logs / "app.log")',
        "DeliveryLoop",
        "VoiceHudOverlay",
        "StudioTrayIcon",
        "hotkey_rig.reset_voice()",
        "event_loop.request_stop()",
        # 快捷键组件失败经现有 error 事件进底栏，事件流每次连上重发（main() 需要真实 Qt/平台/单例锁，测试里跑不了，只能扫接线）
        "on_error=status.set_hotkey_error",
        "on_connected=status.resend_on_connect",
        "set_windows_app_user_model_id(APP_USER_MODEL_ID)",
        "apply_qt_app_icon(app, paths)",
    ):
        assert needed in src
    assert src.index("set_windows_app_user_model_id(APP_USER_MODEL_ID)") < src.index("QApplication(")
    assert src.index("QApplication(") < src.index("apply_qt_app_icon(app, paths)")
    assert src.index("event_loop.request_stop()") < src.index("api.close()") < src.index("event_loop.stop(")


def test_daemon_load_bindings_includes_fifth_hotkey_annotate(tmp_path: Path):
    import json

    import daemon

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"hotkey_annotate": "ctrl+shift+a"}), encoding="utf-8")
    bindings = daemon.load_bindings(cfg_path)
    assert bindings.annotate == "ctrl+shift+a"


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


def _load_entry_daemon():
    """按文件路径加载冻结入口（避免与 site-packages 的 PyPI `packaging` 库同名冲突）。"""
    path = ROOT / "packaging" / "pyinstaller" / "entry_daemon.py"
    spec = importlib.util.spec_from_file_location("ovi_entry_daemon", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entry_daemon_dispatches_five_modes_and_handles_none_stdio(monkeypatch):
    entry = _load_entry_daemon()
    seen: list[tuple[str, list[str]]] = []

    def make(name: str, code: int):
        def _handler(rest: list[str]) -> int:
            seen.append((name, list(rest)))
            return code

        return _handler

    def _daemon() -> int:
        seen.append(("daemon", []))
        return 0

    kw = {
        "run_child_guard": make("child_guard", 11),
        "run_server": make("server", 12),
        "run_setup_worker": make("setup_worker", 13),
        "run_window": make("window", 14),
        "run_daemon": _daemon,
    }
    assert entry.dispatch(["app", INTERNAL_CHILD_GUARD_FLAG, "--mode", "watch"], **kw) == 11
    assert entry.dispatch(["app", INTERNAL_SERVER_FLAG, "--factory", "app:create_production_app"], **kw) == 12
    assert entry.dispatch(["app", INTERNAL_SETUP_WORKER_FLAG, "--kind", "ollama_pull"], **kw) == 13
    assert entry.dispatch(["app", INTERNAL_WINDOW_FLAG, "--url", "https://127.0.0.1:17945/"], **kw) == 14
    assert entry.dispatch(["app"], **kw) == 0
    assert entry.dispatch(["app", "--background"], **kw) == 0
    assert entry.dispatch(["app", "-psn_0_12345"], **kw) == 0
    assert seen == [
        ("child_guard", ["--mode", "watch"]),
        ("server", ["--factory", "app:create_production_app"]),
        ("setup_worker", ["--kind", "ollama_pull"]),
        ("window", ["--url", "https://127.0.0.1:17945/"]),
        ("daemon", []),
        ("daemon", []),
        ("daemon", []),
    ]

    # _run_server 将 uvicorn Click 的 SystemExit 转为整型退出码
    import uvicorn

    def fake_uvicorn_main(*, args, prog_name):
        assert args == ["--port", "17945"] and prog_name == "uvicorn"
        raise SystemExit(3)

    monkeypatch.setattr(uvicorn.main, "main", fake_uvicorn_main)
    assert entry._run_server(["--port", "17945"]) == 3

    # Windows console=False 冻结态下 stdout/stderr 为 None 时 flush_stdio 不报错
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    entry.flush_stdio()


def test_entry_daemon_does_not_preimport_pyside6_on_import_or_internal_dispatch():
    entry_path = ROOT / "packaging" / "pyinstaller" / "entry_daemon.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys; "
                "s = importlib.util.spec_from_file_location('ovi_entry_daemon', sys.argv[1]); "
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "bad = [k for k in ('PySide6', 'daemon', 'uvicorn', 'webview') if k in sys.modules]; "
                "assert not bad, f'preimported: {bad}'"
            ),
            str(entry_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr

    # 真实 CLI 子进程验证 --internal-child-guard 派发（--parent 1 立即返回 1 且绝不 exec）
    if sys.platform.startswith("linux"):
        guard = subprocess.run(
            [
                sys.executable,
                str(entry_path),
                INTERNAL_CHILD_GUARD_FLAG,
                "--mode",
                "pdeathsig",
                "--parent",
                "1",
                "--",
                "echo",
                "never",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert guard.returncode == 1 and "已退出" in guard.stderr
