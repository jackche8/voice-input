#!/usr/bin/env python3
"""临时起一个隔离的 v3 ASGI 沙箱，监听 127.0.0.1 随机端口（避开 _v3.isolated.FORBIDDEN_PORTS：live 三端口与 Ollama 官方端口）。不改服务源码。

ready / meta 只在 uvicorn 确认已监听之后写入，避免调用方在端口尚未 accept 时就开始 fetch。
注入测试用 setup/lan 替身（一键装齐用真实顺序任务，只替换安装 worker），不探测本机真实环境、不启动官方软件、不安装 qrcode。
挂载 static/v3，使 /setup /voice /openapi.snapshot.json 走真实新路由。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path
from threading import Lock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from _contracts import (  # noqa: E402  仓库根目录需先加入 sys.path
    AsrSetupStatus,
    ComponentName,
    ComponentState,
    ErrorCode,
    LogicError,
    OllamaSetupStatus,
    SetupCheckResponse,
    SetupComponentState,
    SetupProgress,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.component_routes import ComponentRegistry  # noqa: E402
from _v3.isolated import FORBIDDEN_PORTS  # noqa: E402
from _v3.setup_sequence import InstallSequence, SequenceFacts  # noqa: E402


def _pick_port() -> int:
    for _ in range(20):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("无法分配非保留端口")


class SandboxTasks:
    """内存安装任务（替身只替换 worker 进程）：同 kind 在 running 时幂等返回，每读一次推进进度，第二次读到 succeeded。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[str, SetupTask] = {}
        self._ticks: dict[str, int] = {}
        self._by_kind: dict[SetupTaskKind, str] = {}
        self._seq = 0

    def start(self, kind: SetupTaskKind) -> SetupTask:
        with self._lock:
            existing = self._tasks.get(self._by_kind.get(kind, ""))
            if existing is not None and existing.state == SetupTaskState.running:
                return existing
            self._seq += 1
            task = SetupTask(
                task_id="sandbox-task-" + str(self._seq),
                kind=kind,
                state=SetupTaskState.running,
                progress=SetupProgress(completed_bytes=0, total_bytes=100, percent=0, stage="start"),
                message="sandbox running",
                started_at="2026-11-10T00:00:00+00:00",
            )
            self._tasks[task.task_id] = task
            self._ticks[task.task_id] = 0
            self._by_kind[kind] = task.task_id
            return task

    def get(self, task_id: str) -> SetupTask:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise LogicError(ErrorCode.not_found, "没有这个任务")
            if task.state != SetupTaskState.running:
                return task
            self._ticks[task_id] += 1
            percent = min(100, self._ticks[task_id] * 50)
            update = {"progress": SetupProgress(completed_bytes=percent, total_bytes=100, percent=percent, stage="download")}
            if self._ticks[task_id] >= 2:
                update.update(state=SetupTaskState.succeeded, message="sandbox completed",
                              finished_at="2026-11-10T00:00:01+00:00")
            task = task.model_copy(update=update)
            self._tasks[task_id] = task
            return task

    def succeeded(self, kind: SetupTaskKind) -> bool:
        with self._lock:
            task = self._tasks.get(self._by_kind.get(kind, ""))
            return task is not None and task.state == SetupTaskState.succeeded


class SandboxSetup:
    """设置替身：真实一键装齐顺序任务（InstallSequence）+ 真实组件登记表，只替换安装 worker 与本机探测。"""

    def __init__(self, components: ComponentRegistry, state_file: Path) -> None:
        self.tasks = SandboxTasks()
        self._components = components
        done = self.tasks.succeeded
        self.sequence = InstallSequence(
            tasks=self.tasks,
            facts=SequenceFacts(
                ollama_installed=lambda: done(SetupTaskKind.ollama_install),
                ollama_has_model=lambda: done(SetupTaskKind.ollama_pull),
                asr_runtime_installed=lambda: done(SetupTaskKind.asr_runtime_install),
                asr_weights_complete=lambda: done(SetupTaskKind.asr_download),
            ),
            components=components,
            state_file=state_file,
            poll_s=0.05,
            wait_ollama_s=5.0,
        )

    def _running(self, name: ComponentName) -> bool:
        status = self._components.get(name)
        return status is not None and status.state == ComponentState.running

    def check(self) -> SetupCheckResponse:
        done = self.tasks.succeeded
        weights = done(SetupTaskKind.asr_download)
        has_model = done(SetupTaskKind.ollama_pull)
        ollama_installed = done(SetupTaskKind.ollama_install)
        return SetupCheckResponse(
            session_type="sandbox",
            asr=AsrSetupStatus(
                model_name="Qwen/Qwen3-ASR-1.7B",
                status=SetupComponentState.ready if weights else SetupComponentState.missing,
                weights_present=weights,
                sidecar_running=self._running(ComponentName.asr),
                message="沙箱识别权重已在" if weights else "沙箱探测：未接生产设备",
            ),
            ollama=OllamaSetupStatus(
                model_name="qwen3.5:4b",
                installed=ollama_installed,
                version="sandbox" if ollama_installed else None,
                running=self._running(ComponentName.ollama),
                model_status=SetupComponentState.ready if has_model else SetupComponentState.missing,
                message="沙箱模型已就绪" if has_model else "沙箱探测：未接生产设备",
            ),
            capabilities=[
                {"id": "clipboard", "label": "剪贴板", "available": False, "required": True, "detail": "沙箱未提供剪贴板"}
            ],
            components=self._components.snapshot(),
            install_all=self.sequence.status(),
        )


class SandboxLan:
    """不调用 qrcode，返回固定测试网地址与遮罩。"""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def describe(self):
        masked = (self.token[:3] + "****" + self.token[-3:]) if len(self.token) > 6 else "****"
        url = "https://192.0.2.8:" + str(self.port) + "/voice?token=" + self.token
        url_masked = "https://192.0.2.8:" + str(self.port) + "/voice?token=" + masked
        qr_svg = "<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'></svg>"
        return {
            "port": self.port,
            "token": self.token,
            "token_masked": masked,
            "entries": [
                {
                    "ip": "192.0.2.8",
                    "url": url,
                    "url_masked": url_masked,
                    "qr_svg": qr_svg,
                }
            ],
            "message": "",
        }


def main() -> int:
    meta_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "v3-asgi-meta.json"
    sandbox_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="v3-ui-asgi-"))
    sandbox_root.mkdir(parents=True, exist_ok=True)

    from _contracts import SttResponse
    from _v3.runtime import build_sandbox_runtime
    from _v3_app import create_app
    import uvicorn

    port = _pick_port()
    if port in FORBIDDEN_PORTS:
        raise SystemExit(f"拒绝使用保留端口 {port}（{sorted(FORBIDDEN_PORTS)}）")

    def transcribe(audio: bytes, language=None, context=None):
        return SttResponse(text="sandbox transcript", language=language or "zh")

    token = "ui-business-sandbox-token"
    runtime = build_sandbox_runtime(
        root=sandbox_root,
        token=token,
        port=port,
        transcribe=transcribe,
    )
    runtime.setup = SandboxSetup(runtime.components, sandbox_root / "install_all.json")
    runtime.lan = SandboxLan(port, token)
    static_v3 = REPO / "static" / "v3"
    app = create_app(runtime, static_v3=static_v3)
    meta = {
        "host": "127.0.0.1",
        "port": port,
        "pid": os.getpid(),
        "root": str(sandbox_root),
        "ready": False,
    }

    def write_ready() -> None:
        meta["ready"] = True
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    class ListeningServer(uvicorn.Server):
        """startup 在 create_server 成功并把 self.started 置 True 之后才回调。"""

        def __init__(self, config: uvicorn.Config, on_listening):
            super().__init__(config)
            self._on_listening = on_listening

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            if not self.started:
                raise RuntimeError("uvicorn 启动完成但未进入监听，拒绝写出 ready/meta")
            self._on_listening()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = ListeningServer(config, write_ready)

    def stop(*_args):
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.run()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit("ASGI 沙箱启动失败：" + str(exc)) from exc
    if not meta["ready"]:
        raise SystemExit("ASGI 沙箱在未进入监听的情况下结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
