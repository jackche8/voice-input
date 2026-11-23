"""运行日志与关于页面 API 真实路由测试：仅本机鉴权、脱敏读/列/清、活跃安装 409 双检、排障汇总与 About。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from _contracts import (
    ComponentName,
    ComponentState,
    InstallAllState,
    InstallAllStatus,
    InstallStep,
    InstallStepKind,
    InstallStepState,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.auth import access_mode
from _v3_app import build_sandbox_runtime, create_app
from tests.api.conftest import TEST_TOKEN, lan_client, lan_headers, local_client, local_headers
from tests.api.setup_fixtures import FakeWorld, build_setup, task


def _sandbox_app(tmp_path: Path, *, setup=None):
    root = tmp_path / "sb"
    runtime = build_sandbox_runtime(root=root, token=TEST_TOKEN, setup=setup)
    app = create_app(runtime)
    return root, runtime, app


def test_logs_and_about_routes_are_local_only_and_csrf_protected(tmp_path: Path):
    for method, path in (
        ("GET", "/api/v1/logs"),
        ("GET", "/api/v1/logs/"),
        ("GET", "/api/v1/logs/diagnostic-summary"),
        ("GET", "/api/v1/logs/app.log"),
        ("DELETE", "/api/v1/logs/app.log"),
        ("GET", "/api/v1/about"),
        ("GET", "/api/v1/about/"),
    ):
        assert access_mode(method, path) == "local", (method, path)

    _root, _rt, app = _sandbox_app(tmp_path)
    remote = lan_client(app)
    public = TestClient(app, client=("8.8.8.8", 5000), base_url="https://8.8.8.8:17945")
    local = local_client(app)

    for method, path in (
        ("GET", "/api/v1/logs"),
        ("GET", "/api/v1/logs/diagnostic-summary"),
        ("GET", "/api/v1/logs/app.log"),
        ("DELETE", "/api/v1/logs/app.log"),
        ("GET", "/api/v1/about"),
    ):
        assert remote.request(method, path, headers=lan_headers()).status_code == 403
        assert public.request(method, path, headers={"X-Voice-Token": TEST_TOKEN}).status_code == 403
        assert local.request(method, path, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert local.request(method, path, headers={"Origin": "https://evil.example:17945"}).status_code == 403


def test_logs_list_read_redaction_and_error_codes(tmp_path: Path):
    root, _rt, app = _sandbox_app(tmp_path)
    logs_dir = root / "logs"
    (logs_dir / "app.log").write_text(
        f"2026-11-23 [INFO] first line\n"
        f"2026-11-23 [WARN] proxy http://alice:pw@127.0.0.1:7890\n"
        f"\x1b[31m2026-11-23 [ERROR] leaked {TEST_TOKEN}\x1b[0m\n",
        encoding="utf-8",
    )
    (logs_dir / "setup-asr_download.log").write_text("2026-11-23 [INFO] canon download\n", encoding="utf-8")
    (logs_dir / "setup-asr-download.log").write_text("2026-11-23 [INFO] legacy ignored\n", encoding="utf-8")

    outside = tmp_path / "outside.log"
    outside.write_text("secret\n", encoding="utf-8")
    (logs_dir / "server.log").symlink_to(outside)

    client = local_client(app)
    # 符号链接导致 server.log 读失败 → 500 logs_io（校验 ERROR_STATUS[ErrorCode.logs_io] == 500）
    sym_resp = client.get("/api/v1/logs/server.log")
    assert sym_resp.status_code == 500 and sym_resp.json()["error"] == "logs_io"

    (logs_dir / "server.log").unlink()
    listed = client.get("/api/v1/logs")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["ok"] is True
    assert body["log_max_lines"] == 2000
    assert body["allowed_max_lines"] == [500, 2000, 5000]
    names = [s["name"] for s in body["sources"]]
    assert len(names) == 9 and "setup-asr_download.log" in names and "setup-asr-download.log" not in names
    app_meta = next(s for s in body["sources"] if s["name"] == "app.log")
    assert app_meta["exists"] is True and app_meta["has_error"] is True and app_meta["has_warn"] is True

    read_resp = client.get("/api/v1/logs/app.log?lines=2")
    assert read_resp.status_code == 200, read_resp.text
    read_body = read_resp.json()
    assert read_body["max_lines"] == 2 and read_body["line_count"] == 2 and read_body["truncated"] is True
    joined = "\n".join(e["text"] for e in read_body["entries"])
    assert TEST_TOKEN not in joined and "alice:pw" not in joined
    assert "http://***@127.0.0.1:7890" in joined
    assert [e["line_no"] for e in read_body["entries"]] == [1, 2]

    # 参数越界 → 400 invalid_request；未知或旧连字符名 → 404 not_found
    for bad_q in ("0", "5001"):
        bad = client.get(f"/api/v1/logs/app.log?lines={bad_q}")
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_request"
    for bad_name in ("setup-asr-download.log", "unknown.log"):
        nf = client.get(f"/api/v1/logs/{bad_name}")
        assert nf.status_code == 404 and nf.json()["error"] == "not_found"


def test_logs_clear_success_and_active_setup_conflict_dual_check(tmp_path: Path):
    root, runtime, app = _sandbox_app(tmp_path)
    logs_dir = root / "logs"
    app_log = logs_dir / "app.log"
    app_log.write_text("line1\nline2\n", encoding="utf-8")
    backup = logs_dir / "app.log.1"
    backup.write_text("old\n", encoding="utf-8")
    size_before = app_log.stat().st_size

    client = local_client(app)
    # 1) 正常清空（setup=None 时亦不报错）
    ok = client.delete("/api/v1/logs/app.log", headers=local_headers())
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "name": "app.log", "cleared": True, "bytes_before": size_before}
    assert app_log.stat().st_size == 0 and not backup.exists()

    # 2) 未知文件 404
    nf = client.delete("/api/v1/logs/no-such.log", headers=local_headers())
    assert nf.status_code == 404 and nf.json()["error"] == "not_found"

    # 3) 双检分支一：tasks.latest(kind) 处于 running → 409 conflict
    pull_log = logs_dir / "setup-ollama_pull.log"
    pull_log.write_text("pulling...\n", encoding="utf-8")
    running_pull = task(SetupTaskState.running, "拉取中", kind=SetupTaskKind.ollama_pull)
    runtime.setup = build_setup(FakeWorld(), {SetupTaskKind.ollama_pull: running_pull})
    conflict1 = client.delete("/api/v1/logs/setup-ollama_pull.log", headers=local_headers())
    assert conflict1.status_code == 409 and conflict1.json()["error"] == "conflict"
    assert pull_log.read_text(encoding="utf-8") == "pulling...\n"

    # 4) 双检分支二：tasks.latest 为 None 但 sequence.status() 当前处于 asr_download running → 409 conflict
    dl_log = logs_dir / "setup-asr_download.log"
    dl_log.write_text("downloading...\n", encoding="utf-8")
    steps = [
        InstallStep(kind=k, state=InstallStepState.running if k == InstallStepKind.asr_download else InstallStepState.done)
        for k in InstallStepKind
    ]
    seq_status = InstallAllStatus(state=InstallAllState.running, current=InstallStepKind.asr_download, steps=steps)
    runtime.setup = SimpleNamespace(
        tasks=SimpleNamespace(latest=lambda _kind: None),
        sequence=SimpleNamespace(status=lambda: seq_status),
    )
    conflict2 = client.delete("/api/v1/logs/setup-asr_download.log", headers=local_headers())
    assert conflict2.status_code == 409 and conflict2.json()["error"] == "conflict"
    assert dl_log.read_text(encoding="utf-8") == "downloading...\n"


def test_diagnostic_summary_and_about_routes_redaction_and_uninjected_failure(tmp_path: Path):
    root, runtime, app = _sandbox_app(tmp_path)
    (root / "logs" / "app.log").write_text(f"2026 [INFO] token={TEST_TOKEN}\n", encoding="utf-8")
    client = local_client(app)

    # 上报含敏感信息的组件原因，校验 diagnostic-summary 优先于 /{name} 且全量脱敏
    report = {
        "components": [
            {
                "name": n.value,
                "state": ComponentState.backing_off.value if n == ComponentName.ollama else ComponentState.running.value,
                "reason": f"proxy http://u:p@127.0.0.1:7890 {TEST_TOKEN}" if n == ComponentName.ollama else None,
                "since": "2026-11-23T00:00:00+00:00",
            }
            for n in ComponentName
        ]
    }
    assert client.post("/api/v1/components", json=report, headers=local_headers()).status_code == 202

    summary = client.get("/api/v1/logs/diagnostic-summary")
    assert summary.status_code == 200, summary.text
    sbody = summary.json()
    assert sbody["ok"] is True and sbody["included_files"] == ["app.log"]
    assert TEST_TOKEN not in sbody["text"] and "u:p" not in sbody["text"]
    assert "http://***@127.0.0.1:7890" in sbody["text"]

    about = client.get("/api/v1/about")
    assert about.status_code == 200, about.text
    abody = about.json()
    assert abody["ok"] is True and abody["session_type"] == "sandbox"
    assert str(root) in abody["logs_dir"] and str(root) in abody["data_root"]
    assert abody["links"]["repo_url"].startswith("https://github.com/")

    # 未注入服务显式返回 503 dependency_failed
    runtime.logs = None
    runtime.about = None
    assert client.get("/api/v1/logs").status_code == 503
    assert client.get("/api/v1/about").status_code == 503
