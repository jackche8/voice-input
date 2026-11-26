"""三个本地组件的启动规格：命令、环境、工作目录、日志、端口、就绪探针、已安装判定、显存预检、超时。

唯一的组件启动命令来源（取代 systemd 单元模板）。顺序即拉起顺序：ollama → asr → server。
asr 的变体（cuda / cpu / mps）只读运行环境清单 `paths.asr_manifest` 的 `variant`；清单可能在应用运行中才装好，
所以调用方在每次拉起前重新 build_specs。端口来自 `_ports.local_ports(environ)`（默认即已有常量）。
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from _contracts import ComponentName
from _jobs_client import build_tls_context
from _paths import INTERNAL_SERVER_FLAG, AppPaths, asr_runtime_root, internal_subcommand
from _platform.child_process import set_env_var
from _platform.protocol import OsName
from _platform.system_proxy import SystemProxyReader, merge_proxy_env, read_os_system_proxy
from _ports import LocalPorts, local_ports
from _supervision.gpu_preflight import asr_gpu_preflight, asr_windows_cuda_preflight
from _v3.setup_probe import ASR_VARIANTS, asr_variant, probe_asr_sidecar, probe_asr_weights, probe_ollama

READY_TIMEOUT_S = 3.0
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
Runner = Callable[..., subprocess.CompletedProcess]


def _asr_variant_args(variant: str | None, os_name: OsName) -> tuple[str, ...]:
    if variant == "cuda":
        if os_name == "windows":
            return ("--backend", "transformers", "--device", "cuda")
        return ("--backend", "vllm", "--gpu-memory-utilization", "0.45", "--max-model-len", "4096")
    if variant == "cpu":
        return ("--backend", "transformers", "--device", "cpu")
    if variant == "mps":
        return ("--backend", "transformers", "--device", "mps")
    return ()


@dataclass(frozen=True)
class ReadyResult:
    ok: bool
    detail: str | None = None


def _no_preflight() -> str | None:
    return None


@dataclass(frozen=True)
class ComponentSpec:
    name: ComponentName
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    log_path: Path
    port: int
    ready: Callable[[], ReadyResult]  # 只读探针，单次 READY_TIMEOUT_S 超时
    installed: Callable[[], str | None]  # None = 已安装；否则缺什么
    start_timeout_s: float
    stop_timeout_s: float
    preflight: Callable[[], str | None] = _no_preflight  # None = 通过；否则拒启原因


def _http_ready(url: str, check: Callable[[httpx.Client], ReadyResult], *, verify: Callable[[], object] = lambda: True) -> ReadyResult:
    try:
        with httpx.Client(base_url=url, timeout=READY_TIMEOUT_S, trust_env=False, verify=verify()) as client:
            return check(client)
    except (httpx.HTTPError, ValueError, OSError) as exc:  # 还没起来属于正常的「未就绪」，原因随启动超时一起报出
        return ReadyResult(False, f"{type(exc).__name__}: {exc}")


def _ollama(
    paths: AppPaths,
    environ: Mapping[str, str],
    ports: LocalPorts,
    *,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
) -> ComponentSpec:
    def ready() -> ReadyResult:
        def check(client: httpx.Client) -> ReadyResult:
            probe = probe_ollama(binary=paths.ollama_binary, client=client, model="")
            return ReadyResult(probe.running, probe.error)

        return _http_ready(ports.ollama_url, check)

    def installed() -> str | None:
        return None if paths.ollama_binary.is_file() else f"未安装 Ollama（{paths.ollama_binary} 不存在）"

    env = merge_proxy_env(environ, os_name=paths.os_name, read_system_proxy=read_system_proxy)
    set_env_var(env, "OLLAMA_HOST", f"127.0.0.1:{ports.ollama}", os_name=paths.os_name, overwrite=True)
    set_env_var(env, "OLLAMA_MODELS", str(paths.ollama_models), os_name=paths.os_name, overwrite=True)
    set_env_var(env, "OLLAMA_KEEP_ALIVE", DEFAULT_OLLAMA_KEEP_ALIVE, os_name=paths.os_name, overwrite=True)
    return ComponentSpec(
        name=ComponentName.ollama, argv=(str(paths.ollama_binary), "serve"), env=env, cwd=paths.data_root,
        log_path=paths.logs / "ollama.log", port=ports.ollama, ready=ready, installed=installed,
        start_timeout_s=60.0, stop_timeout_s=10.0,
    )


def _asr(
    paths: AppPaths,
    environ: Mapping[str, str],
    ports: LocalPorts,
    *,
    which: Callable[[str], str | None],
    run: Runner,
    frozen: bool | None = None,
    machine: str | None = None,
) -> ComponentSpec:
    try:
        variant, variant_error = asr_variant(paths), None
    except ValueError as exc:
        variant, variant_error = None, str(exc)
    asr_root = asr_runtime_root(paths.program_root, frozen=frozen)
    argv = (
        str(paths.asr_python), "-m", "runtimes.asr", "serve", "--host", "127.0.0.1", "--port", str(ports.asr),
        "--model", str(paths.asr_model_dir), "--load", "--warmup", *_asr_variant_args(variant, paths.os_name),
    )
    env = {**environ, "PYTHONPATH": str(asr_root), "HF_HOME": str(paths.hf_home), "HF_HUB_OFFLINE": "1",
           "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}

    def ready() -> ReadyResult:
        def check(client: httpx.Client) -> ReadyResult:
            probe = probe_asr_sidecar(client)
            return ReadyResult(probe.loaded, probe.error or (None if probe.loaded else "识别服务已响应，模型尚未加载完"))

        return _http_ready(ports.asr_url, check)

    def installed() -> str | None:
        if variant_error:
            return variant_error
        if variant is None:
            return f"未安装识别运行环境（{paths.asr_manifest} 不存在）"
        weights = probe_asr_weights(paths.asr_model_dir)
        return None if weights.complete else f"识别模型不完整：{'、'.join(weights.missing)}"

    def preflight() -> str | None:
        if paths.os_name == "windows":
            result = asr_windows_cuda_preflight(run=run, which=which, environ=env, machine=machine)
        else:
            result = asr_gpu_preflight(argv, run=run, which=which, environ=env, os_name=paths.os_name)
        return None if result.ok else result.reason

    return ComponentSpec(
        name=ComponentName.asr, argv=argv, env=env, cwd=asr_root, log_path=paths.logs / "asr.log",
        port=ports.asr, ready=ready, installed=installed, start_timeout_s=300.0, stop_timeout_s=30.0,
        preflight=preflight if variant == "cuda" else _no_preflight,
    )


def _server(
    paths: AppPaths,
    environ: Mapping[str, str],
    ports: LocalPorts,
    *,
    frozen: bool | None = None,
    executable: str | None = None,
) -> ComponentSpec:
    def ready() -> ReadyResult:
        def check(client: httpx.Client) -> ReadyResult:
            resp = client.get("/api/v1/status")
            return ReadyResult(resp.status_code == 200, None if resp.status_code == 200 else f"HTTP {resp.status_code}")

        # 证书由调用方在拉起前生成，所以 TLS 上下文在探测时才建（只信任本应用 CA）
        return _http_ready(ports.server_origin, check, verify=lambda: build_tls_context(paths.cert))

    argv = (
        *internal_subcommand(INTERNAL_SERVER_FLAG, ("-m", "uvicorn"), frozen=frozen, executable=executable),
        "--factory", "app:create_production_app", "--host", "0.0.0.0",
        "--port", str(ports.server), "--ssl-keyfile", str(paths.key), "--ssl-certfile", str(paths.cert),
    )
    return ComponentSpec(
        name=ComponentName.server, argv=argv, env={**environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}, cwd=paths.program_root,
        log_path=paths.logs / "server.log", port=ports.server, ready=ready, installed=lambda: None,
        start_timeout_s=60.0, stop_timeout_s=10.0,
    )


def build_specs(
    paths: AppPaths,
    environ: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    frozen: bool | None = None,
    executable: str | None = None,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
    machine: str | None = None,
) -> tuple[ComponentSpec, ...]:
    ports = local_ports(environ)
    return (
        _ollama(paths, environ, ports, read_system_proxy=read_system_proxy),
        _asr(paths, environ, ports, which=which, run=run, frozen=frozen, machine=machine),
        _server(paths, environ, ports, frozen=frozen, executable=executable),
    )


__all__ = [
    "ASR_VARIANTS",
    "ComponentSpec",
    "DEFAULT_OLLAMA_KEEP_ALIVE",
    "READY_TIMEOUT_S",
    "ReadyResult",
    "asr_variant",
    "build_specs",
]
