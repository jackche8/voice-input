"""显式启动 sidecar。默认只绑 127.0.0.1:17946；不带 --load 不会占 GPU。

支持 --backend {auto,vllm,transformers}：Linux+CUDA+vLLM 环境自动走 vllm，
Windows / macOS / 纯 CPU 环境自动走跨平台 transformers（CUDA / MPS / CPU）。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from runtimes.asr.process_guard import ChildProcessGuard
from runtimes.asr.protocol import (
    DEFAULT_BACKEND,
    DEFAULT_CHUNK_SIZE_SEC,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_HOST,
    DEFAULT_MAX_MODEL_LEN,
    DEFAULT_PORT,
    DEFAULT_UNFIXED_CHUNK_NUM,
    DEFAULT_UNFIXED_TOKEN_NUM,
    SUPPORTED_BACKENDS,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qwen3-ASR sidecar (loopback only)")
    parser.add_argument("command", choices=("serve",), help="serve 启动 HTTP 宿主")
    parser.add_argument("--host", default=DEFAULT_HOST, help="只允许 127.0.0.1 / ::1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--backend",
        choices=SUPPORTED_BACKENDS,
        default=DEFAULT_BACKEND,
        help="推理后端：auto（默认，vLLM+CUDA 可用时用 vllm，否则 transformers）、vllm 或 transformers",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Transformers 后端显式设备（cuda / mps / cpu）；省略则自动探测",
    )
    parser.add_argument("--load", action="store_true", help="启动即加载模型；省略则拒绝推理")
    parser.add_argument("--warmup", action="store_true", help="加载后用短静音预热")
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=DEFAULT_GPU_MEMORY_UTILIZATION,
    )
    parser.add_argument("--max-model-len", type=int, default=DEFAULT_MAX_MODEL_LEN)
    parser.add_argument("--chunk-size-sec", type=float, default=DEFAULT_CHUNK_SIZE_SEC)
    parser.add_argument("--unfixed-chunk-num", type=int, default=DEFAULT_UNFIXED_CHUNK_NUM)
    parser.add_argument("--unfixed-token-num", type=int, default=DEFAULT_UNFIXED_TOKEN_NUM)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("sidecar 只允许绑定回环地址", file=sys.stderr)
        return 2

    guard = ChildProcessGuard()
    guard.enter_new_session()
    state: dict = {"server": None}

    def request_stop(signum) -> None:
        server = state["server"]
        if server is not None:
            server.should_exit = True
            if hasattr(server, "force_exit"):
                server.force_exit = True
            return
        guard.remember()
        guard.reap()
        os._exit(128 + int(signum))

    guard.install(request_stop=request_stop)
    engine = None
    exit_code = 0
    try:
        from runtimes.asr.engine import DEFAULT_MODEL_PATH, OfficialEngine
        from runtimes.asr.server import create_sidecar_app

        engine = OfficialEngine(
            model_path=args.model or DEFAULT_MODEL_PATH,
            backend=args.backend,
            device=args.device,
            gpu_memory_utilization=args.gpu_memory_utilization,
            max_model_len=args.max_model_len,
            chunk_size_sec=args.chunk_size_sec,
            unfixed_chunk_num=args.unfixed_chunk_num,
            unfixed_token_num=args.unfixed_token_num,
        )
        if args.load or args.warmup:
            engine.load(warmup=bool(args.warmup))
            guard.remember()
        app = create_sidecar_app(engine)
        import uvicorn

        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        state["server"] = server

        def install_and_chain() -> None:
            signal.signal(signal.SIGTERM, lambda s, _f: request_stop(s))
            signal.signal(signal.SIGINT, lambda s, _f: request_stop(s))

        server.install_signal_handlers = install_and_chain
        server.run()
    finally:
        if engine is not None:
            try:
                engine.unload()
            except Exception:
                pass
        guard.remember()
        result = guard.reap()
        leftover = result.get("survived") or []
        if leftover:
            print(f"sidecar 子进程未全部回收: {leftover}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
