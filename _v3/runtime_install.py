"""识别运行环境安装动作（在安装 worker 进程里执行）：固定版本 uv → 托管 CPython → venv → 带 hash 的锁定依赖 → 清单。

变体：
- Linux 且 nvidia-smi 读到至少一张 GPU → cuda（vLLM）；
- Windows x64 且满足单源准入（总显存 >= 4608 MiB、计算能力 >= 7.0、驱动 >= 570）→ cuda（PyTorch cu128 + Transformers）；
  仅 nvidia-smi 不存在或硬件不合格时合法返回 cpu；驱动 < 570 或探测故障显式报错，绝不静默降级 CPU；
- macOS arm64 → mps（不调用 nvidia-smi）；其余 → cpu。
清单 `paths.asr_manifest` 最后写，存在且匹配目标变体即「已安装」；支持 cpu → cuda 升级与损坏清单重装。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from _atomic_file import atomic_write_bytes
from _contracts import ErrorCode, LogicError, SetupProgress
from _paths import PROGRAM_ROOT, AppPaths, asr_runtime_root
from _platform.child_process import set_env_var, unset_env_var
from _platform.protocol import OsName
from _platform.system_proxy import SystemProxyReader, merge_proxy_env, read_os_system_proxy
from _supervision.gpu_preflight import (
    NvidiaSmiNotFoundError,
    evaluate_windows_cuda_gpu,
    is_cuda_visible_devices_empty,
    query_gpus,
)
from _v3.release_download import download_verified, extract_archive
from _v3.setup_probe import asr_variant
from _v3.setup_tasks import GIB, ProgressFn, SetupAction
from _v3.uv_runner import INSTALL_TIMEOUT_S, STALL_TIMEOUT_S, dir_bytes, run_uv_streaming

UV_VERSION = "0.11.3"
PYTHON_VERSION = "3.12.13"
UV_RELEASE_BASE = "https://github.com/astral-sh/uv/releases/download"
LOCK_DIR = asr_runtime_root(PROGRAM_ROOT) / "runtimes" / "asr"
HARDWARE_PROBE_TTL_S = 30.0
REMOVING_VENV_PREFIX = ".venv.removing."
Runner = Callable[..., subprocess.CompletedProcess]

_UV_TARGETS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("macos", "arm64"): "aarch64-apple-darwin",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("windows", "amd64"): "x86_64-pc-windows-msvc",
}


@dataclass(frozen=True)
class AsrHardwareProbe:
    """ASR 硬件探测结构化结果：仅 error is None 时 target_variant 有效；故障或旧驱动显式置 error，绝不回落 cpu。"""

    target_variant: str | None
    detail: str
    gpu_name: str | None = None
    driver_version: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.target_variant is not None


class CachedHardwareProbe:
    """线程安全 30s TTL 硬件探测缓存（成功与失败均缓存，支持升级起止主动失效）。"""

    def __init__(
        self,
        probe: Callable[[], AsrHardwareProbe],
        *,
        ttl_s: float = HARDWARE_PROBE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: tuple[float, AsrHardwareProbe] | None = None

    def get(self) -> AsrHardwareProbe:
        with self._lock:
            now = self._monotonic()
            if self._cached is not None and (now - self._cached[0]) < self._ttl_s:
                return self._cached[1]
            result = self._probe()
            self._cached = (self._monotonic(), result)
            return result

    __call__ = get

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None


def choose_variant(os_name: OsName, machine: str, *, has_nvidia_gpu: bool) -> str:
    arch = machine.strip().lower()
    if os_name == "linux" and has_nvidia_gpu:
        return "cuda"
    if os_name == "windows" and has_nvidia_gpu and arch in ("amd64", "x86_64"):
        return "cuda"
    if os_name == "macos" and arch == "arm64":
        return "mps"
    return "cpu"


def probe_asr_hardware(
    os_name: OsName,
    machine: str,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> AsrHardwareProbe:
    """结构化探测目标识别变体：macOS 不调 nvidia-smi；Windows 走单源准入；故障/旧驱动绝不静默降 CPU。"""
    arch = machine.strip().lower()
    if os_name == "macos":
        if arch == "arm64":
            return AsrHardwareProbe(target_variant="mps", detail="Apple Silicon（MPS）")
        return AsrHardwareProbe(target_variant="cpu", detail=f"macOS {machine} 使用 CPU")
    if os_name == "windows":
        if arch not in ("amd64", "x86_64"):
            return AsrHardwareProbe(target_variant="cpu", detail=f"Windows {machine} 架构不支持 CUDA 加速，使用 CPU")
        if is_cuda_visible_devices_empty(environ, os_name="windows"):
            return AsrHardwareProbe(target_variant="cpu", detail="CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU，使用 CPU")
        try:
            gpus = query_gpus(run=run, which=which, os_name="windows", environ=environ)
        except NvidiaSmiNotFoundError as exc:
            return AsrHardwareProbe(target_variant="cpu", detail=f"未检测到 NVIDIA GPU（{exc}）")
        except ValueError as exc:
            return AsrHardwareProbe(target_variant=None, detail=str(exc), error=f"NVIDIA GPU 探测失败：{exc}")
        verdict = evaluate_windows_cuda_gpu(gpus, machine=machine, environ=environ, check_free_vram=False)
        gpu = verdict.gpu
        gpu_name = None if gpu is None else gpu.name
        driver_version = (gpu.driver_version or None) if gpu is not None else None
        if verdict.status == "eligible":
            return AsrHardwareProbe(
                target_variant="cuda",
                detail=verdict.reason,
                gpu_name=gpu_name,
                driver_version=driver_version,
            )
        if verdict.status == "unsupported_cpu":
            return AsrHardwareProbe(
                target_variant="cpu",
                detail=verdict.reason,
                gpu_name=gpu_name,
                driver_version=driver_version,
            )
        return AsrHardwareProbe(
            target_variant=None,
            detail=verdict.reason,
            gpu_name=gpu_name,
            driver_version=driver_version,
            error=verdict.reason,
        )
    if os_name == "linux":
        if is_cuda_visible_devices_empty(environ, os_name="linux"):
            return AsrHardwareProbe(target_variant="cpu", detail="CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU，使用 CPU")
        try:
            gpus = query_gpus(run=run, which=which, os_name="linux", environ=environ)
        except NvidiaSmiNotFoundError as exc:
            return AsrHardwareProbe(target_variant="cpu", detail=f"未检测到 NVIDIA GPU（{exc}）")
        except ValueError as exc:
            return AsrHardwareProbe(target_variant=None, detail=str(exc), error=f"NVIDIA GPU 探测失败：{exc}")
        names = "、".join(gpu.name for gpu in gpus)
        return AsrHardwareProbe(
            target_variant="cuda",
            detail=f"检测到 GPU：{names}",
            gpu_name=gpus[0].name,
            driver_version=gpus[0].driver_version or None,
        )
    return AsrHardwareProbe(target_variant="cpu", detail=f"{os_name}/{machine} 使用 CPU")


def detect_variant(
    os_name: OsName,
    machine: str,
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """返回（变体，判定依据）；探测故障或驱动 < 570 时显式抛 LogicError，绝不静默回落 CPU。"""
    probe = probe_asr_hardware(os_name, machine, run=run, which=which, environ=environ)
    if probe.error is not None or probe.target_variant is None:
        raise LogicError(ErrorCode.dependency_failed, probe.error or probe.detail)
    return probe.target_variant, probe.detail


def variant_disk_bytes(os_name: OsName, variant: str) -> int:
    """单源安装峰值磁盘预算（不含 disk_shortfall 自带的 1 GiB 余量）。"""
    if variant == "cuda":
        return 12 * GIB if os_name == "windows" else 16 * GIB
    if variant in ("cpu", "mps"):
        return 4 * GIB
    raise ValueError(f"未知的识别运行环境变体：{variant!r}")


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.stat(_existing_ancestor(a)).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return False


def asr_runtime_disk_budget(paths: AppPaths, os_name: OsName, variant: str) -> tuple[Path, int]:
    """计算目标变体净磁盘需求：扣减同卷可回收/可复用的旧 asr_venv、.venv.removing* 与 uv/cache，钳制 >= 0。"""
    gross = variant_disk_bytes(os_name, variant)
    reclaimable = 0
    if paths.asr_venv.exists() and _same_volume(paths.asr_venv, paths.asr_venv):
        reclaimable += dir_bytes(paths.asr_venv)
    parent = paths.asr_venv.parent
    if parent.is_dir() and _same_volume(parent, paths.asr_venv):
        for entry in parent.iterdir():
            if entry.name.startswith(REMOVING_VENV_PREFIX):
                reclaimable += dir_bytes(entry)
    uv_cache = paths.uv_dir / "cache"
    if uv_cache.exists() and _same_volume(uv_cache, paths.asr_venv):
        reclaimable += dir_bytes(uv_cache)
    return paths.asr_venv, max(0, gross - reclaimable)


def uv_asset(os_name: OsName, machine: str) -> str:
    target = _UV_TARGETS.get((os_name, machine.lower()))
    if target is None:
        raise LogicError(ErrorCode.dependency_failed, f"uv 没有 {os_name}/{machine} 的官方发行包")
    return f"uv-{target}.zip" if os_name == "windows" else f"uv-{target}.tar.gz"


def lock_file(
    variant: str,
    *,
    os_name: OsName | None = None,
    program_root: Path | None = None,
    frozen: bool | None = None,
) -> Path:
    filename = "requirements-cuda-win.lock" if (variant == "cuda" and os_name == "windows") else f"requirements-{variant}.lock"
    if program_root is None and frozen is None:
        return LOCK_DIR / filename
    base = PROGRAM_ROOT if program_root is None else program_root
    return asr_runtime_root(base, frozen=frozen) / "runtimes" / "asr" / filename


def asr_runtime_cleanup(paths: AppPaths, *, rename_retries: int = 10, retry_delay_s: float = 0.2) -> None:
    """原子重命名旧 venv（锁死直接抛 PermissionError 且绝不删清单），成功后删清单并清扫全部 .venv.removing.*。"""
    if paths.asr_venv.exists():
        staging = paths.asr_venv.parent / f"{REMOVING_VENV_PREFIX}{os.getpid()}.{uuid.uuid4().hex[:8]}"
        attempts = max(1, int(rename_retries))
        for attempt in range(attempts):
            try:
                os.rename(paths.asr_venv, staging)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(retry_delay_s)
    paths.asr_manifest.unlink(missing_ok=True)
    parent = paths.asr_venv.parent
    if parent.is_dir():
        for entry in sorted(parent.iterdir(), key=lambda p: p.name):
            if not entry.name.startswith(REMOVING_VENV_PREFIX):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)


def _uv_binary(paths: AppPaths, os_name: OsName) -> Path:
    return paths.uv_dir / ("uv.exe" if os_name == "windows" else "uv")


def _install_uv(paths: AppPaths, os_name: OsName, machine: str, client: httpx.Client, report: ProgressFn, run: Runner) -> Path:
    uv = _uv_binary(paths, os_name)
    if uv.is_file():
        return uv
    asset = uv_asset(os_name, machine)
    base = f"{UV_RELEASE_BASE}/{UV_VERSION}"
    paths.uv_dir.mkdir(parents=True, exist_ok=True)
    archive = paths.uv_dir / f".{asset}.download"
    staging = paths.uv_dir / ".staging"
    try:
        download_verified(client, url=f"{base}/{asset}", sums_url=f"{base}/{asset}.sha256", asset=asset, dest=archive,
                          report=report, stage=f"下载 uv {UV_VERSION}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        extract_archive(archive, asset, staging, run=run)
        found = next(staging.rglob(uv.name), None)
        if found is None:
            raise LogicError(ErrorCode.dependency_failed, f"发行包 {asset} 内没有 {uv.name}")
        os.replace(found, uv)
        uv.chmod(0o755)
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    return uv


def _build_uv_env(
    environ: Mapping[str, str],
    *,
    paths: AppPaths,
    os_name: OsName,
    read_system_proxy: SystemProxyReader,
) -> dict[str, str]:
    """单源合并系统代理与 `uv` 隔离/网络参数（Windows 大小写去重，保留用户显式非空网络覆盖）。"""
    env = merge_proxy_env(environ, os_name=os_name, read_system_proxy=read_system_proxy)
    set_env_var(env, "UV_PYTHON_INSTALL_DIR", str(paths.python_dir), os_name=os_name, overwrite=True)
    set_env_var(env, "UV_CACHE_DIR", str(paths.uv_dir / "cache"), os_name=os_name, overwrite=True)
    set_env_var(env, "UV_PYTHON_PREFERENCE", "only-managed", os_name=os_name, overwrite=True)
    set_env_var(env, "UV_NO_CONFIG", "1", os_name=os_name, overwrite=True)
    set_env_var(env, "PYTHONUTF8", "1", os_name=os_name, overwrite=True)
    set_env_var(env, "NO_COLOR", "1", os_name=os_name, overwrite=True)
    unset_env_var(env, "FORCE_COLOR", os_name=os_name)
    unset_env_var(env, "CLICOLOR_FORCE", os_name=os_name)
    set_env_var(env, "UV_SYSTEM_CERTS", "1", os_name=os_name, overwrite=False)
    set_env_var(env, "UV_CONCURRENT_DOWNLOADS", "8", os_name=os_name, overwrite=False)
    set_env_var(env, "UV_HTTP_TIMEOUT", "120", os_name=os_name, overwrite=False)
    return env


def asr_runtime_install_action(
    *,
    paths: AppPaths,
    os_name: OsName,
    machine: str,
    variant: Callable[[], tuple[str, str]],
    environ: Mapping[str, str],
    client_factory: Callable[[], httpx.Client],
    run: Runner = subprocess.run,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    read_system_proxy: SystemProxyReader = read_os_system_proxy,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
    on_replace_start: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SetupAction:
    def install(report: ProgressFn) -> str:
        chosen, why = variant()
        try:
            existing = asr_variant(paths)
        except ValueError:
            existing = None  # 清单损坏：允许重装并重建清单
        if existing == chosen:
            raise LogicError(ErrorCode.conflict, f"识别运行环境已安装（{chosen}，{paths.asr_manifest}），未重复安装")
        report(SetupProgress(stage=f"识别运行环境变体 {chosen}：{why}"))
        lock = lock_file(chosen, os_name=os_name, program_root=paths.program_root)
        uv = _uv_binary(paths, os_name)
        if not uv.is_file():
            with client_factory() as client:
                uv = _install_uv(paths, os_name, machine, client, report, run)
        # 只用托管 Python、不读用户的 uv/pip 配置（锁文件已写明索引地址），并同步系统代理
        env = _build_uv_env(environ, paths=paths, os_name=os_name, read_system_proxy=read_system_proxy)
        uv_cache = paths.uv_dir / "cache"
        log_path = paths.logs / "setup-asr_runtime_install.log"
        run_uv_streaming(
            [str(uv), "python", "install", "--color", "never", PYTHON_VERSION],
            env=env,
            what=f"安装 Python {PYTHON_VERSION}",
            report=report,
            os_name=os_name,
            log_path=log_path,
            primary_dir=paths.python_dir,
            activity_dirs=(paths.python_dir, uv_cache),
            popen=popen,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise LogicError(ErrorCode.dependency_failed, "安装已中止，未替换既有识别运行环境")
        if on_replace_start is not None:
            on_replace_start()
        try:
            asr_runtime_cleanup(paths)
        except (PermissionError, OSError) as exc:
            raise LogicError(
                ErrorCode.dependency_failed,
                f"清理旧识别运行环境失败（可能有进程仍占用文件，请关闭占用后重试）：{exc}",
            ) from exc
        run_uv_streaming(
            [str(uv), "venv", "--color", "never", "--clear", "--python", PYTHON_VERSION, str(paths.asr_venv)],
            env=env,
            what="创建识别运行环境",
            report=report,
            os_name=os_name,
            log_path=log_path,
            primary_dir=None,
            activity_dirs=(paths.asr_venv,),
            popen=popen,
            cancel_event=cancel_event,
        )
        run_uv_streaming(
            [
                str(uv),
                "pip",
                "install",
                "--color",
                "never",
                "--python",
                str(paths.asr_python),
                "--require-hashes",
                "-r",
                str(lock),
            ],
            env=env,
            what=f"安装识别依赖（{chosen}）",
            report=report,
            os_name=os_name,
            log_path=log_path,
            primary_dir=uv_cache,
            activity_dirs=(uv_cache, paths.asr_venv),
            popen=popen,
            cancel_event=cancel_event,
        )
        run_uv_streaming(
            [str(uv), "cache", "clean", "--color", "never"],
            env=env,
            what="清理下载缓存",
            report=report,
            os_name=os_name,
            log_path=log_path,
            primary_dir=None,
            activity_dirs=(uv_cache,),
            popen=popen,
            cancel_event=cancel_event,
        )  # 已装进 venv 的文件不受影响；失败重试时缓存保留可续用
        if cancel_event is not None and cancel_event.is_set():
            raise LogicError(ErrorCode.dependency_failed, "安装已中止，未写入运行环境清单")
        manifest = {"variant": chosen, "python": PYTHON_VERSION, "uv": UV_VERSION,
                    "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "installed_at": clock()}
        atomic_write_bytes(paths.asr_manifest, json.dumps(manifest, ensure_ascii=False).encode("utf-8"), 0o644)
        return f"识别运行环境已安装（{chosen}，Python {PYTHON_VERSION}）"

    return install


__all__ = [
    "HARDWARE_PROBE_TTL_S",
    "INSTALL_TIMEOUT_S",
    "PYTHON_VERSION",
    "REMOVING_VENV_PREFIX",
    "STALL_TIMEOUT_S",
    "UV_VERSION",
    "AsrHardwareProbe",
    "CachedHardwareProbe",
    "asr_runtime_cleanup",
    "asr_runtime_disk_budget",
    "asr_runtime_install_action",
    "choose_variant",
    "detect_variant",
    "lock_file",
    "probe_asr_hardware",
    "uv_asset",
    "variant_disk_bytes",
]
