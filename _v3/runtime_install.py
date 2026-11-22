"""识别运行环境安装动作（在安装 worker 进程里执行）：固定版本 uv → 托管 CPython → venv → 带 hash 的锁定依赖 → 清单。

变体：Linux 且 nvidia-smi 读到至少一张 GPU → cuda；macOS arm64 → mps；其余（含 Windows + NVIDIA，已知限制）→ cpu。
清单 `paths.asr_manifest` 最后写，存在即「已安装」（看护层与一键装齐都按它判断）；任何一步失败都不写清单。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import httpx

from _atomic_file import atomic_write_bytes
from _contracts import ErrorCode, LogicError, SetupProgress
from _paths import PROGRAM_ROOT, AppPaths, asr_runtime_root
from _platform.protocol import OsName
from _supervision.gpu_preflight import query_gpus
from _v3.release_download import download_verified, extract_archive
from _v3.setup_tasks import GIB, ProgressFn, SetupAction

UV_VERSION = "0.11.3"
PYTHON_VERSION = "3.12.13"
UV_RELEASE_BASE = "https://github.com/astral-sh/uv/releases/download"
LOCK_DIR = asr_runtime_root(PROGRAM_ROOT) / "runtimes" / "asr"
# 安装峰值 = venv + uv 下载缓存（装完即清缓存）。cpu 实测 venv 1.6 GB + 缓存 1.7 GB + Python 0.1 GB（八.18）；
# cuda 按现有 venv-asr 7.2 GB 同比估算；mps 按 cpu 计。
VARIANT_DISK_BYTES: Mapping[str, int] = {"cuda": 16 * GIB, "cpu": 4 * GIB, "mps": 4 * GIB}
INSTALL_TIMEOUT_S = 3600
Runner = Callable[..., subprocess.CompletedProcess]

_UV_TARGETS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("macos", "arm64"): "aarch64-apple-darwin",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("windows", "amd64"): "x86_64-pc-windows-msvc",
}


def choose_variant(os_name: OsName, machine: str, *, has_nvidia_gpu: bool) -> str:
    if os_name == "linux" and has_nvidia_gpu:
        return "cuda"
    if os_name == "macos" and machine.lower() == "arm64":
        return "mps"
    return "cpu"


def detect_variant(os_name: OsName, machine: str, *, run: Runner, which: Callable[[str], str | None]) -> tuple[str, str]:
    """返回（变体，判定依据）；依据写进任务进度，未检测到 GPU 的原因可见。"""
    try:
        gpus = query_gpus(run=run, which=which)
    except ValueError as exc:
        return choose_variant(os_name, machine, has_nvidia_gpu=False), f"未检测到 NVIDIA GPU（{exc}）"
    names = "、".join(gpu.name for gpu in gpus)
    return choose_variant(os_name, machine, has_nvidia_gpu=True), f"检测到 GPU：{names}"


def uv_asset(os_name: OsName, machine: str) -> str:
    target = _UV_TARGETS.get((os_name, machine.lower()))
    if target is None:
        raise LogicError(ErrorCode.dependency_failed, f"uv 没有 {os_name}/{machine} 的官方发行包")
    return f"uv-{target}.zip" if os_name == "windows" else f"uv-{target}.tar.gz"


def lock_file(variant: str, *, program_root: Path | None = None, frozen: bool | None = None) -> Path:
    if program_root is None and frozen is None:
        return LOCK_DIR / f"requirements-{variant}.lock"
    base = PROGRAM_ROOT if program_root is None else program_root
    return asr_runtime_root(base, frozen=frozen) / "runtimes" / "asr" / f"requirements-{variant}.lock"


def asr_runtime_cleanup(paths: AppPaths) -> None:
    """重试前删除整个识别 venv 与清单（托管 Python 与 uv 本身可复用，不删）。"""
    paths.asr_manifest.unlink(missing_ok=True)
    if paths.asr_venv.exists():
        shutil.rmtree(paths.asr_venv)


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


def _run_uv(run: Runner, argv: list[str], env: Mapping[str, str], what: str) -> None:
    proc = run(argv, env=dict(env), capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise LogicError(ErrorCode.dependency_failed, f"{what}失败（退出码 {proc.returncode}）：{tail}")


def asr_runtime_install_action(
    *,
    paths: AppPaths,
    os_name: OsName,
    machine: str,
    variant: Callable[[], tuple[str, str]],
    environ: Mapping[str, str],
    client_factory: Callable[[], httpx.Client],
    run: Runner = subprocess.run,
    clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
) -> SetupAction:
    def install(report: ProgressFn) -> str:
        if paths.asr_manifest.is_file():
            raise LogicError(ErrorCode.conflict, f"识别运行环境已安装（{paths.asr_manifest}），未重复安装")
        chosen, why = variant()
        report(SetupProgress(stage=f"识别运行环境变体 {chosen}：{why}"))
        lock = lock_file(chosen, program_root=paths.program_root)
        with client_factory() as client:
            uv = _install_uv(paths, os_name, machine, client, report, run)
        # 只用托管 Python、不读用户的 uv/pip 配置（锁文件已写明索引地址）
        env = {**environ, "UV_PYTHON_INSTALL_DIR": str(paths.python_dir), "UV_CACHE_DIR": str(paths.uv_dir / "cache"),
               "UV_PYTHON_PREFERENCE": "only-managed", "UV_NO_CONFIG": "1"}
        report(SetupProgress(stage=f"安装 Python {PYTHON_VERSION}"))
        _run_uv(run, [str(uv), "python", "install", PYTHON_VERSION], env, f"安装 Python {PYTHON_VERSION} ")
        report(SetupProgress(stage="创建识别运行环境"))
        _run_uv(run, [str(uv), "venv", "--python", PYTHON_VERSION, str(paths.asr_venv)], env, "创建识别运行环境")
        report(SetupProgress(stage=f"安装识别依赖（{chosen}）"))
        _run_uv(run, [str(uv), "pip", "install", "--python", str(paths.asr_python), "--require-hashes", "-r", str(lock)],
                env, f"安装识别依赖（{chosen}）")
        report(SetupProgress(stage="清理下载缓存"))
        _run_uv(run, [str(uv), "cache", "clean"], env, "清理下载缓存")  # 已装进 venv 的文件不受影响；失败重试时缓存保留可续用
        manifest = {"variant": chosen, "python": PYTHON_VERSION, "uv": UV_VERSION,
                    "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "installed_at": clock()}
        atomic_write_bytes(paths.asr_manifest, json.dumps(manifest, ensure_ascii=False).encode("utf-8"), 0o644)
        return f"识别运行环境已安装（{chosen}，Python {PYTHON_VERSION}）"

    return install


__all__ = [
    "PYTHON_VERSION",
    "UV_VERSION",
    "VARIANT_DISK_BYTES",
    "asr_runtime_cleanup",
    "asr_runtime_install_action",
    "choose_variant",
    "detect_variant",
    "lock_file",
    "uv_asset",
]
