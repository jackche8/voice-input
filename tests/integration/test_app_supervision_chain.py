"""R1 真实链路（阶段一场景）：真实看护运行器按真实组件规格拉起——假 Ollama / 假识别服务（本地 HTTP 小程序，装在
AppPaths 规定的位置）+ 真实服务端（uvicorn + app:create_production_app，https）；组件快照经真实 PlatformStatus 上报到
真实服务端，GET /api/setup/check 读到三项 running；杀掉识别服务 → 看护层清会话、退避后重新拉起；停止后进程无残留。

全部端口经 OVI_PORT_* 改到临时空闲端口（不碰在用的 17945–17947），数据根、配置根、运行目录都在测试临时目录。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import httpx

from _contracts import ComponentName
from _desktop.api import DesktopApi
from _desktop.status import PlatformStatus
from _desktop.supervisor import create_supervisor
from _jobs_client import build_tls_context
from _paths import default_app_paths
from _platform.certs import ensure_tls_certificates
from _procfs import session_members

FAKE_SERVICE = """#!{python}
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if "--port" in sys.argv:  # 识别服务：-m runtimes.asr serve --host … --port N …
    port, routes = int(sys.argv[sys.argv.index("--port") + 1]), {{"/health": {{"ok": True, "loaded": True}}}}
else:  # Ollama：serve，端口来自 OLLAMA_HOST
    port = int(os.environ["OLLAMA_HOST"].rsplit(":", 1)[1])
    routes = {{"/api/version": {{"version": "0.40.16"}}, "/api/tags": {{"models": []}}}}
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(routes.get(self.path, {{}})).encode()
        self.send_response(200 if self.path in routes else 404)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def _free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for sock in socks:
        sock.bind(("127.0.0.1", 0))
    ports = [sock.getsockname()[1] for sock in socks]
    for sock in socks:
        sock.close()
    return ports


def _wait_until(predicate, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"等待超时：{what}"
        time.sleep(0.2)


def _install_fakes(paths) -> None:
    script = FAKE_SERVICE.format(python=sys.executable)
    for target in (paths.ollama_binary, paths.asr_python):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
    paths.asr_manifest.write_text(json.dumps({"variant": "cpu"}), encoding="utf-8")
    paths.asr_model_dir.mkdir(parents=True)
    (paths.asr_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (paths.asr_model_dir / "model.safetensors").write_bytes(b"w")


class Tray:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_state(self, state, tooltip=None):
        self.calls.append(("state", state, tooltip))

    def set_server_stopped(self, stopped):
        self.calls.append(("server_stopped", stopped))


def test_supervisor_runs_real_components_reports_to_real_server_recovers_and_cleans_up(tmp_path: Path):
    server_port, asr_port, ollama_port = _free_ports(3)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "OVI_"))},
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "OVI_PORT_SERVER": str(server_port),
        "OVI_PORT_ASR": str(asr_port),
        "OVI_PORT_OLLAMA": str(ollama_port),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    paths = default_app_paths(env)
    _install_fakes(paths)
    ensure_tls_certificates(paths.cert_dir)  # 与应用入口相同：本机 API 客户端要用它校验
    origin = f"https://127.0.0.1:{server_port}"
    api = DesktopApi(origin, cert=paths.cert)
    status = PlatformStatus(api.post_platform_event, post_components=api.post_components)
    tray = Tray()
    sup = create_supervisor(paths, env, status=status, tray=tray, ui=lambda fn: fn())
    client = httpx.Client(base_url=origin, verify=build_tls_context(paths.cert), trust_env=False, timeout=5)

    def server_view():
        try:
            body = client.get("/api/setup/check").json()
        except httpx.HTTPError:
            return None
        return {c["name"]: c for c in body["components"]}

    sup.start()
    sessions: set[int] = set()
    try:
        view = _wait_until(lambda: (v := server_view()) and all(v[n]["state"] == "running" for n in ("ollama", "asr", "server"))
                           and v, 120, "服务端读到三项 running")
        sessions |= {view[n]["pid"] for n in view}
        assert {c.name for c in sup.snapshot()} == set(ComponentName)

        old_asr = view["asr"]["pid"]
        os.kill(old_asr, signal.SIGKILL)  # 识别服务崩溃：清会话 → 退避 → 重新拉起，并重新上报
        view = _wait_until(lambda: (v := server_view()) and v["asr"]["state"] == "running" and v["asr"]["pid"] != old_asr
                           and v, 60, "识别服务被重新拉起")
        assert view["asr"]["failures"] == 1
        sessions.add(view["asr"]["pid"])
        assert ("server_stopped", True) not in tray.calls
    finally:
        sup.stop(timeout_s=60)
        client.close()
        status.close()
        api.close()
    assert all(session_members(sid) == [] for sid in sessions), {sid: session_members(sid) for sid in sessions}
    assert json.loads(paths.children_record.read_text(encoding="utf-8"))["children"] == {}
