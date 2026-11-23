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
from _desktop.event_stream import ProductEventLoop, dispatch_product_event
from _desktop.status import PlatformStatus
from _desktop.supervisor import create_supervisor
from _jobs_client import build_tls_context
from _paths import PROGRAM_ROOT, default_app_paths
from _platform.certs import ensure_tls_certificates
from _procfs import session_members

FAKE_SERVICE = """#!{python}
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if "--port" in sys.argv:  # 识别服务：-m runtimes.asr serve --host … --port N …
    log_path = os.environ.get("OVI_TEST_ASR_ARGV_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv}}) + "\\n")
    port, routes = int(sys.argv[sys.argv.index("--port") + 1]), {{"/health": {{"ok": True, "loaded": True}}}}
else:  # Ollama：serve，端口来自 OLLAMA_HOST
    port = int(os.environ["OLLAMA_HOST"].rsplit(":", 1)[1])
    models = json.loads(os.environ.get("OVI_TEST_OLLAMA_MODELS") or "[]")
    routes = {{"/api/version": {{"version": "0.40.16"}}, "/api/tags": {{"models": models}}}}
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


FAKE_UV_SCRIPT = """#!{python}
import os, shutil, sys, time
from pathlib import Path

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "venv":
    venv_dir = Path(args[-1])
    py_path = venv_dir / "bin" / "python"
    py_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(os.environ["OVI_TEST_FAKE_ASR_SRC"], py_path)
    py_path.chmod(0o755)
elif len(args) >= 2 and args[0] == "pip" and args[1] == "install":
    marker = Path(os.environ["OVI_TEST_UV_ENTERED"])
    marker.write_text(" ".join(args), encoding="utf-8")
    gate = Path(os.environ["OVI_TEST_UV_RELEASE"])
    deadline = time.monotonic() + 30.0
    while not gate.exists():
        if time.monotonic() > deadline:
            sys.stderr.write("timeout waiting for test uv gate\\n")
            sys.exit(2)
        time.sleep(0.05)
sys.exit(0)
"""

FAKE_NVIDIA_SMI = """#!{python}
print("0, GPU-chain, 16303, 15000, 572.16, 8.9, RTX 4070")
"""


def test_real_chain_cpu_to_cuda_upgrade_stops_replaces_and_restarts_asr(tmp_path: Path):
    """无 mock 真实升级全链：真 uvicorn HTTPS + 真 Supervisor + 真 ProductEventLoop SSE + 真 setup_worker 子进程。"""
    server_port, asr_port, ollama_port = _free_ports(3)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    smi_bin = fake_bin / "nvidia-smi"
    smi_bin.write_text(FAKE_NVIDIA_SMI.format(python=sys.executable), encoding="utf-8")
    smi_bin.chmod(0o755)

    asr_argv_log = tmp_path / "asr-argv.log"
    uv_entered = tmp_path / "uv-entered"
    uv_release = tmp_path / "uv-release"
    fake_asr_src = tmp_path / "fake-asr-bin"
    fake_asr_src.write_text(FAKE_SERVICE.format(python=sys.executable), encoding="utf-8")
    fake_asr_src.chmod(0o755)

    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "OVI_"))},
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(PROGRAM_ROOT),
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "OVI_PORT_SERVER": str(server_port),
        "OVI_PORT_ASR": str(asr_port),
        "OVI_PORT_OLLAMA": str(ollama_port),
        "OVI_TEST_ASR_ARGV_LOG": str(asr_argv_log),
        "OVI_TEST_OLLAMA_MODELS": json.dumps([{"name": "qwen3.5:4b", "model": "qwen3.5:4b", "size": 3300000000}]),
        "OVI_TEST_FAKE_ASR_SRC": str(fake_asr_src),
        "OVI_TEST_UV_ENTERED": str(uv_entered),
        "OVI_TEST_UV_RELEASE": str(uv_release),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    paths = default_app_paths(env)
    # 安装假 Ollama + 初始 CPU 假 ASR + 假 uv（不触网，真实执行 asr_runtime_cleanup 与 manifest.json 写入）
    _install_fakes(paths)
    paths.uv_dir.mkdir(parents=True, exist_ok=True)
    fake_uv = paths.uv_dir / "uv"
    fake_uv.write_text(FAKE_UV_SCRIPT.format(python=sys.executable), encoding="utf-8")
    fake_uv.chmod(0o755)

    ensure_tls_certificates(paths.cert_dir)
    origin = f"https://127.0.0.1:{server_port}"
    headers = {"Origin": origin}
    api = DesktopApi(origin, cert=paths.cert)
    status = PlatformStatus(api.post_platform_event, post_components=api.post_components)
    tray = Tray()
    sup = create_supervisor(paths, env, status=status, tray=tray, ui=lambda fn: fn())

    def _on_component_command(action: str, component: str) -> None:
        if action == "retry":
            sup.retry(component)
        elif action == "stop":
            sup.stop_component(component)

    class _NoopHud:
        mode = "idle"

        def __init__(self) -> None:
            self.errors: list[str] = []

        def set_idle(self) -> None:
            pass

        def set_error(self, message: str) -> None:
            self.errors.append(message)

    hud = _NoopHud()
    event_loop = ProductEventLoop(
        iter_events=api.iter_product_events,
        on_event=lambda ev: dispatch_product_event(
            ev, hud=hud, tray=tray, on_component_command=_on_component_command
        ),
        on_connected=status.resend_on_connect,
    )
    client = httpx.Client(base_url=origin, verify=build_tls_context(paths.cert), trust_env=False, timeout=5)

    def fetch_check():
        try:
            return client.get("/api/setup/check").json()
        except httpx.HTTPError:
            return None

    sup.start()
    event_loop.start()
    sessions: set[int] = set()
    try:
        # 1) 初始态：三组件均 running，旧 ASR 以 --device cpu 运行，设置页识别到 CPU→GPU 可升级
        initial = _wait_until(
            lambda: (chk := fetch_check())
            and len(chk["components"]) == 3
            and all(c["state"] == "running" for c in chk["components"])
            and chk,
            120,
            "三组件均进入 running",
        )
        comp_map = {c["name"]: c for c in initial["components"]}
        sessions |= {c["pid"] for c in initial["components"] if c["pid"]}
        old_asr_pid = comp_map["asr"]["pid"]
        assert "CPU" in (initial["asr"]["device_label"] or "") and "RTX 4070" in (initial["asr"]["device_label"] or "")

        # 2) 点击「一键装齐」：真 SSE 下发 stop → Supervisor 停止旧 ASR → 真 setup_worker 替换 venv
        started = client.post("/api/setup/install-all", headers=headers)
        assert started.status_code == 202

        # 等待真 setup_worker 进入 uv pip install（此时旧 ASR 已停且旧 venv 已原子清理）
        _wait_until(lambda: uv_entered.is_file(), 60, "setup_worker 进入 Step 4 uv pip install")
        assert session_members(old_asr_pid) == [], "升级替换期间旧 ASR 进程必须已退出"

        # 3) Step 4 运行中：POST /api/v1/components/asr/retry 必须返回 409 Conflict
        blocked = client.post("/api/v1/components/asr/retry", headers=headers)
        assert blocked.status_code == 409 and blocked.json()["error"] == "conflict"

        # 4) 放行 uv → setup_worker 写 cuda 清单 → Step 5 跳过权重 → 真 SSE 发 retry → Supervisor 重建 spec 拉起新 GPU ASR
        uv_release.write_text("ok", encoding="utf-8")
        done = _wait_until(
            lambda: (chk := fetch_check()) and chk["install_all"]["state"] in ("done", "failed") and chk,
            60,
            "一键装齐升级达到终态",
        )
        assert done["install_all"]["state"] == "done", done["install_all"]
        assert done["install_all"]["steps"][3]["state"] == "done"
        assert json.loads(paths.asr_manifest.read_text(encoding="utf-8"))["variant"] == "cuda"

        restarted = _wait_until(
            lambda: (chk := fetch_check())
            and any(c["name"] == "asr" and c["state"] == "running" and c["pid"] != old_asr_pid for c in chk["components"])
            and chk,
            60,
            "新 CUDA ASR 服务重新拉起",
        )
        new_asr_pid = next(c["pid"] for c in restarted["components"] if c["name"] == "asr")
        sessions.add(new_asr_pid)
        assert restarted["asr"]["device_label"] == "CUDA GPU"

        # 核实旧进程用 --device cpu，新进程按 cuda 清单切到 --backend vllm（Linux）
        launches = [json.loads(line) for line in asr_argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(launches) >= 2
        assert "--device" in launches[0]["argv"] and "cpu" in launches[0]["argv"]
        assert "--backend" in launches[-1]["argv"] and "vllm" in launches[-1]["argv"]
        assert hud.errors == []
    finally:
        uv_release.write_text("ok", encoding="utf-8")
        event_loop.request_stop()
        sup.stop(timeout_s=60)
        client.close()
        api.close()
        event_loop.stop(timeout=1.0)
        status.close()
    assert all(session_members(sid) == [] for sid in sessions), {sid: session_members(sid) for sid in sessions}
    assert json.loads(paths.children_record.read_text(encoding="utf-8"))["children"] == {}
