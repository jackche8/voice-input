"""新启动 ASR sidecar 前的 GPU/显存预检与 Windows CUDA 准入（逻辑层，只读 nvidia-smi 与启动参数，不导入 torch/vllm）。

与 vLLM 0.14.0 同源（Linux CUDA）：
- vllm/v1/worker/gpu_worker.py:233-234 `init_snapshot = MemorySnapshot(device)`；
  `requested_memory = request_memory(init_snapshot, cache_config)`；
- vllm/v1/worker/utils.py:253-273 `request_memory`：
  `requested = ceil(total_memory * gpu_memory_utilization)`，`free_memory < requested` 即 ValueError 拒启；
- vllm/utils/mem_utils.py MemorySnapshot.measure：`free_memory, total_memory = torch.cuda.mem_get_info(device)`。

Windows CUDA（PyTorch 2.9.1 cu128 + Transformers CUDA）：
- 安装态、设置页探测、启动预检共用唯一准入函数 `evaluate_windows_cuda_gpu()`：
  x64 架构 + 唯一目标卡 + 总显存 >= 4608 MiB + 计算能力 >= 7.0（cu128 已移除 Pascal/Maxwell）+ 驱动 >= 570.00；
  启动预检额外要求空闲显存 >= 4608 MiB（对应 Qwen3-ASR-1.7B bf16 实测峰值 4378 MiB）。
- 严格区分「无 nvidia-smi（`NvidiaSmiNotFoundError`）」与「存在但执行失败/超时/不可解析（`ValueError`）」，绝不把探测故障误判成无 GPU。
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from _platform.child_process import windows_subprocess_kwargs
from _platform.detect import detect_os
from _platform.protocol import OsName

Runner = Callable[..., subprocess.CompletedProcess]
MIB = 1024 * 1024
ASR_MODULE = "runtimes.asr"

MIN_WIN_CUDA_DRIVER = (570, 0)
MIN_WIN_CUDA_COMPUTE_CAP = (7, 0)
MIN_WIN_CUDA_VRAM_MIB = 4608


class NvidiaSmiNotFoundError(ValueError):
    """仅表示系统未找到 nvidia-smi 可执行文件（合法视作无 NVIDIA GPU）。"""


@dataclass(frozen=True)
class GpuMemory:
    index: str
    uuid: str
    name: str
    total_mib: int
    free_mib: int
    driver_version: str = ""
    compute_cap: str = ""


@dataclass(frozen=True)
class PreflightResult:
    """kind：ok（够）/ insufficient（实测不够）/ unknown（无法判断，同样拒启）。"""

    kind: str
    reason: str
    utilization: float | None = None
    required_mib: int | None = None
    free_mib: int | None = None
    total_mib: int | None = None
    gpu: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


@dataclass(frozen=True)
class WindowsCudaEligibility:
    """Windows CUDA 单源判定结果：eligible / unsupported_cpu / error / insufficient。"""

    status: Literal["eligible", "unsupported_cpu", "error", "insufficient"]
    reason: str
    gpu: GpuMemory | None = None
    required_mib: int | None = None


def _env_get(environ: Mapping[str, str], key: str, *, case_insensitive: bool) -> str | None:
    if key in environ:
        return environ[key]
    if case_insensitive:
        target = key.lower()
        for env_key, value in environ.items():
            if env_key.lower() == target:
                return value
    return None


def parse_driver_version(raw: str) -> tuple[int, int] | None:
    """解析 NVIDIA 驱动版本（如 '570.86.16' -> (570, 86)，'570' -> (570, 0)）；无效返回 None。"""
    text = raw.strip()
    if not text:
        return None
    parts = text.split(".")
    if not parts or any(not (part.isascii() and part.isdigit()) for part in parts):
        return None
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return (major, minor)


def parse_compute_cap(raw: str) -> tuple[int, int] | None:
    """解析 CUDA 计算能力（如 '8.6' -> (8, 6)）；无效返回 None。"""
    text = raw.strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) != 2 or any(not (part.isascii() and part.isdigit()) for part in parts):
        return None
    return (int(parts[0]), int(parts[1]))


def is_cuda_visible_devices_empty(
    environ: Mapping[str, str] | None = None,
    *,
    os_name: OsName | None = None,
) -> bool:
    """CUDA_VISIBLE_DEVICES 已显式设置但为空（如 '' / '   ' / ','）时返回 True。"""
    env = os.environ if environ is None else environ
    resolved_os = os_name or detect_os()
    raw_cvd = _env_get(env, "CUDA_VISIBLE_DEVICES", case_insensitive=(resolved_os == "windows"))
    if raw_cvd is None:
        return False
    tokens = [part.strip() for part in raw_cvd.split(",") if part.strip()]
    return not tokens


def find_nvidia_smi(
    *,
    which: Callable[[str], str | None] = shutil.which,
    os_name: OsName | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """查找 nvidia-smi 完整路径：优先按显式 environ 的 PATH（或自定义 which）定位，Windows 依次兜底 System32 与 NVSMI 标准目录。"""
    resolved_os = os_name or detect_os()
    if which is shutil.which and environ is not None:
        env_path = _env_get(environ, "PATH", case_insensitive=(resolved_os == "windows"))
        found = shutil.which("nvidia-smi", path=env_path) if env_path else None
    else:
        found = which("nvidia-smi")
    if found:
        return found
    if resolved_os != "windows":
        return None
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    sysroot = (_env_get(env, "SystemRoot", case_insensitive=True) or "").strip()
    if sysroot:
        candidates.append(Path(sysroot) / "System32" / "nvidia-smi.exe")
    progfiles = (_env_get(env, "ProgramFiles", case_insensitive=True) or "").strip()
    if progfiles:
        candidates.append(Path(progfiles) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def asr_utilization_from_argv(exec_argv: Sequence[str]) -> float:
    """从 sidecar 启动 argv 取实际 gpu_memory_utilization：用 sidecar 自己的 build_parser 解析
    `-m runtimes.asr` 之后的参数。不是 sidecar 命令或参数无法解析抛 ValueError。"""
    from runtimes.asr.__main__ import build_parser

    argv = list(exec_argv)
    try:
        start = next(i for i in range(len(argv) - 1) if argv[i] == "-m" and argv[i + 1] == ASR_MODULE) + 2
    except StopIteration:
        raise ValueError(f"不是 `-m {ASR_MODULE}` sidecar 命令：{' '.join(argv)[:200]}") from None
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            args = build_parser().parse_args(argv[start:])
    except SystemExit:
        raise ValueError(f"sidecar 参数无法解析：{err.getvalue().strip()[-200:]}") from None
    util = float(args.gpu_memory_utilization)
    if not 0.0 < util <= 1.0:
        raise ValueError(f"gpu_memory_utilization={util} 不在 (0, 1]")
    return util


def query_gpus(
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    os_name: OsName | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[GpuMemory]:
    """nvidia-smi 实时读数（单次 7 字段查询）。

    未找到 nvidia-smi 抛 `NvidiaSmiNotFoundError`；找到但调用失败/超时/非 0/无法解析/空列表抛 `ValueError`。
    """
    smi_path = find_nvidia_smi(which=which, os_name=os_name, environ=environ)
    if not smi_path:
        raise NvidiaSmiNotFoundError("未找到 nvidia-smi，无法读取显存")
    cmd = [
        smi_path,
        "--query-gpu=index,uuid,memory.total,memory.free,driver_version,compute_cap,name",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **windows_subprocess_kwargs(encoding="utf-8", errors="replace"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"nvidia-smi 调用失败：{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"nvidia-smi 退出码 {proc.returncode}：{(proc.stderr or proc.stdout or '').strip()[:200]}")
    gpus: list[GpuMemory] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",", 6)]
        try:
            if len(parts) != 7:
                raise ValueError
            gpus.append(
                GpuMemory(
                    index=parts[0],
                    uuid=parts[1],
                    total_mib=int(parts[2]),
                    free_mib=int(parts[3]),
                    driver_version=parts[4],
                    compute_cap=parts[5],
                    name=parts[6],
                )
            )
        except ValueError:
            raise ValueError(f"nvidia-smi 输出无法解析：{line.strip()[:200]!r}") from None
    if not gpus:
        raise ValueError("nvidia-smi 没有列出任何 GPU")
    return gpus


def _select_target_gpu(
    gpus: list[GpuMemory],
    environ: Mapping[str, str] | None = None,
    *,
    os_name: OsName | None = None,
) -> tuple[GpuMemory | None, str | None]:
    """单卡直接选中；多卡仅在 CUDA_VISIBLE_DEVICES 明确指定唯一 1 张 GPU（索引或 UUID）时定位该卡。"""
    listing = "；".join(f"#{g.index} {g.name} 空闲 {g.free_mib}/{g.total_mib} MiB" for g in gpus)
    env = os.environ if environ is None else environ
    resolved_os = os_name or detect_os()
    raw_cvd = _env_get(env, "CUDA_VISIBLE_DEVICES", case_insensitive=(resolved_os == "windows"))
    if raw_cvd is not None:
        if not raw_cvd.strip():
            return None, "CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU"
        tokens = [part.strip() for part in raw_cvd.split(",") if part.strip()]
        if not tokens:
            return None, "CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU"
        if len(tokens) > 1:
            return (
                None,
                f"CUDA_VISIBLE_DEVICES={raw_cvd!r} 指定了多张 GPU（检测到 {len(gpus)} 张：{listing}），无法确定识别服务将使用哪一张，不做假定",
            )
        selector = tokens[0]
        matches = [g for g in gpus if g.index == selector or g.uuid == selector]
        if not matches and selector.startswith(("GPU-", "MIG-")):
            matches = [g for g in gpus if g.uuid.startswith(selector)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return (
                None,
                f"CUDA_VISIBLE_DEVICES={raw_cvd!r} 匹配到多张 GPU（{listing}），无法确定识别服务将使用哪一张，不做假定",
            )
        return (
            None,
            f"CUDA_VISIBLE_DEVICES={raw_cvd!r} 未匹配到任何可用 GPU（检测到：{listing}）",
        )
    if len(gpus) == 1:
        return gpus[0], None
    return (
        None,
        f"检测到 {len(gpus)} 张 GPU（{listing}），无法确定识别服务将使用哪一张，不做假定",
    )


def evaluate_windows_cuda_gpu(
    gpus: list[GpuMemory],
    *,
    machine: str,
    environ: Mapping[str, str] | None = None,
    check_free_vram: bool = False,
) -> WindowsCudaEligibility:
    """Windows CUDA 唯一准入函数（安装态 / 设置页探测 / 启动预检共用）。"""
    if machine.strip().lower() not in ("amd64", "x86_64"):
        return WindowsCudaEligibility(
            status="unsupported_cpu",
            reason=f"Windows {machine} 架构不支持 CUDA 加速，使用 CPU",
        )
    if is_cuda_visible_devices_empty(environ, os_name="windows"):
        return WindowsCudaEligibility(
            status="unsupported_cpu",
            reason="CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU，使用 CPU",
        )
    gpu, reason = _select_target_gpu(gpus, environ=environ, os_name="windows")
    if gpu is None:
        return WindowsCudaEligibility(
            status="error",
            reason=reason or "无法确定目标 GPU",
        )
    if gpu.total_mib < MIN_WIN_CUDA_VRAM_MIB:
        return WindowsCudaEligibility(
            status="unsupported_cpu",
            reason=f"GPU {gpu.name} 总显存 {gpu.total_mib} MiB 小于 {MIN_WIN_CUDA_VRAM_MIB} MiB，使用 CPU",
            gpu=gpu,
            required_mib=MIN_WIN_CUDA_VRAM_MIB,
        )
    cap = parse_compute_cap(gpu.compute_cap)
    if cap is None:
        return WindowsCudaEligibility(
            status="error",
            reason=f"无法解析 GPU（{gpu.name}）的计算能力：{gpu.compute_cap!r}",
            gpu=gpu,
        )
    if cap < MIN_WIN_CUDA_COMPUTE_CAP:
        return WindowsCudaEligibility(
            status="unsupported_cpu",
            reason=f"GPU {gpu.name} 计算能力 {gpu.compute_cap} 低于 PyTorch cu128 最低要求 7.0，使用 CPU",
            gpu=gpu,
        )
    driver = parse_driver_version(gpu.driver_version)
    if driver is None:
        return WindowsCudaEligibility(
            status="error",
            reason=f"检测到 NVIDIA GPU（{gpu.name}），但无法解析驱动版本 {gpu.driver_version!r}，请更新 NVIDIA 驱动到 570 及以上后重试",
            gpu=gpu,
        )
    if driver < MIN_WIN_CUDA_DRIVER:
        return WindowsCudaEligibility(
            status="error",
            reason=(
                f"检测到 NVIDIA GPU（{gpu.name}），当前驱动版本 {gpu.driver_version} 低于 "
                f"PyTorch cu128 要求的 570.00，请先更新 NVIDIA 驱动到 570 及以上后重试"
            ),
            gpu=gpu,
        )
    if check_free_vram and gpu.free_mib < MIN_WIN_CUDA_VRAM_MIB:
        return WindowsCudaEligibility(
            status="insufficient",
            reason=(
                f"显存不足：识别服务需要 {MIN_WIN_CUDA_VRAM_MIB} MiB 空闲显存，"
                f"当前空闲 {gpu.free_mib} MiB（总量 {gpu.total_mib} MiB，{gpu.name}）。请先关闭占用显存的程序"
            ),
            gpu=gpu,
            required_mib=MIN_WIN_CUDA_VRAM_MIB,
        )
    return WindowsCudaEligibility(
        status="eligible",
        reason=f"检测到 GPU：{gpu.name}（驱动 {gpu.driver_version}，计算能力 {gpu.compute_cap}）",
        gpu=gpu,
        required_mib=MIN_WIN_CUDA_VRAM_MIB,
    )


def asr_windows_cuda_preflight(
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    machine: str | None = None,
) -> PreflightResult:
    """Windows CUDA 启动前预检：单目标卡 + 驱动 >= 570 + 计算能力 >= 7.0 + 空闲显存 >= 4608 MiB。"""
    arch = platform.machine() if machine is None else machine
    try:
        gpus = query_gpus(run=run, which=which, os_name="windows", environ=environ)
    except ValueError as exc:
        return PreflightResult(kind="unknown", reason=f"显存探测失败：{exc}")
    verdict = evaluate_windows_cuda_gpu(gpus, machine=arch, environ=environ, check_free_vram=True)
    gpu = verdict.gpu
    fields = dict(
        required_mib=verdict.required_mib,
        free_mib=None if gpu is None else gpu.free_mib,
        total_mib=None if gpu is None else gpu.total_mib,
        gpu=None if gpu is None else f"#{gpu.index} {gpu.name}",
    )
    if verdict.status == "eligible" and gpu is not None:
        return PreflightResult(
            kind="ok",
            reason=(
                f"Windows CUDA 预检通过：需要 {MIN_WIN_CUDA_VRAM_MIB} MiB，"
                f"空闲 {gpu.free_mib}/{gpu.total_mib} MiB，驱动 {gpu.driver_version}，计算能力 {gpu.compute_cap}（{gpu.name}）"
            ),
            **fields,
        )
    if verdict.status == "insufficient":
        return PreflightResult(kind="insufficient", reason=verdict.reason, **fields)
    return PreflightResult(kind="unknown", reason=verdict.reason, **fields)


def asr_gpu_preflight(
    exec_argv: Sequence[str],
    *,
    run: Runner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    os_name: OsName | None = None,
) -> PreflightResult:
    """按 sidecar 实际参数与实时显存判定能否新启动 Linux vLLM ASR。单卡或 CUDA_VISIBLE_DEVICES 指定唯一单卡时判定；其余多卡 → unknown。"""
    try:
        util = asr_utilization_from_argv(exec_argv)
    except ValueError as exc:
        return PreflightResult(kind="unknown", reason=f"无法确定识别服务的显存占用比例：{exc}")
    try:
        gpus = query_gpus(run=run, which=which, os_name=os_name, environ=environ)
    except ValueError as exc:
        return PreflightResult(kind="unknown", reason=f"显存探测失败：{exc}", utilization=util)
    gpu, reason = _select_target_gpu(gpus, environ=environ, os_name=os_name)
    if gpu is None:
        return PreflightResult(
            kind="unknown",
            reason=reason or "无法确定目标 GPU",
            utilization=util,
        )
    required_bytes = math.ceil(gpu.total_mib * MIB * util)  # 同 vllm request_memory：ceil(total × util)
    required_mib = math.ceil(required_bytes / MIB)
    fields = dict(utilization=util, required_mib=required_mib, free_mib=gpu.free_mib, total_mib=gpu.total_mib, gpu=f"#{gpu.index} {gpu.name}")
    if gpu.free_mib * MIB < required_bytes:
        return PreflightResult(
            kind="insufficient",
            reason=(
                f"显存不足：识别服务需要 {required_mib} MiB（{util} × 总量 {gpu.total_mib} MiB），"
                f"当前空闲 {gpu.free_mib} MiB（{gpu.name}）。请先关闭占用显存的程序"
            ),
            **fields,
        )
    return PreflightResult(
        kind="ok",
        reason=f"显存预检通过：需要 {required_mib} MiB（{util} × {gpu.total_mib} MiB），空闲 {gpu.free_mib} MiB（{gpu.name}）",
        **fields,
    )


__all__ = [
    "ASR_MODULE",
    "MIB",
    "MIN_WIN_CUDA_COMPUTE_CAP",
    "MIN_WIN_CUDA_DRIVER",
    "MIN_WIN_CUDA_VRAM_MIB",
    "GpuMemory",
    "NvidiaSmiNotFoundError",
    "PreflightResult",
    "Runner",
    "WindowsCudaEligibility",
    "asr_gpu_preflight",
    "asr_utilization_from_argv",
    "asr_windows_cuda_preflight",
    "evaluate_windows_cuda_gpu",
    "find_nvidia_smi",
    "is_cuda_visible_devices_empty",
    "parse_compute_cap",
    "parse_driver_version",
    "query_gpus",
]
