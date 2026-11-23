"""L4：ASR 新启动前显存预检与 Windows CUDA 单源准入（假 nvidia-smi；不碰 GPU、不导入 torch/vllm）。

Linux CUDA 与 venv-asr 内 vLLM 0.14.0 同源：vllm/v1/worker/utils.py:253-273 request_memory
（requested = ceil(total × gpu_memory_utilization)，free < requested 即拒启）。
Windows CUDA 与 PyTorch 2.9.1 cu128 + Transformers 同源：x64 + 唯一目标卡 + 总显存 >= 4608 MiB
+ 计算能力 >= 7.0 + 驱动 >= 570.00（启动态额外校验空闲显存 >= 4608 MiB）。
"""
from __future__ import annotations

import math
import subprocess
import sys

import pytest

from _supervision.gpu_preflight import (
    MIN_WIN_CUDA_VRAM_MIB,
    NvidiaSmiNotFoundError,
    asr_gpu_preflight,
    asr_utilization_from_argv,
    asr_windows_cuda_preflight,
    evaluate_windows_cuda_gpu,
    find_nvidia_smi,
    query_gpus,
)
from runtimes.asr.protocol import DEFAULT_GPU_MEMORY_UTILIZATION

ARGV = ("/v/bin/python", "-m", "runtimes.asr", "serve", "--host", "127.0.0.1", "--port", "17946", "--load", "--warmup", "--model", "/m/Qwen3-ASR-1.7B")
WHICH = lambda n: f"/usr/bin/{n}"  # noqa: E731


def smi(out: str, rc: int = 0, calls: list | None = None, *, expected_cmd0: str = "/usr/bin/nvidia-smi"):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(list(cmd))
        assert cmd[0] == expected_cmd0
        return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else "NVIDIA-SMI has failed")

    return run


def one_gpu(
    free: int,
    total: int = 16303,
    *,
    driver: str = "572.16",
    compute_cap: str = "8.9",
    name: str = "NVIDIA GeForce RTX 5070 Ti",
) -> str:
    return f"0, GPU-f7da, {total}, {free}, {driver}, {compute_cap}, {name}\n"


def test_default_utilization_comes_from_sidecar_parser_and_protocol_constant():
    assert asr_utilization_from_argv(ARGV) == DEFAULT_GPU_MEMORY_UTILIZATION
    assert asr_utilization_from_argv((*ARGV, "--gpu-memory-utilization", "0.55")) == 0.55


@pytest.mark.parametrize("argv", [("/usr/bin/ollama", "serve"), ("/v/bin/python", "-m", "runtimes.asr", "serve", "--bogus")])
def test_unrecognised_exec_start_is_unknown(argv):
    result = asr_gpu_preflight(argv, run=smi(one_gpu(16000)), which=WHICH)
    assert result.kind == "unknown" and not result.ok


def test_enough_and_boundary_follow_vllm_ceil_comparison():
    required = math.ceil(16303 * DEFAULT_GPU_MEMORY_UTILIZATION)  # MiB 粒度下与 ceil(bytes) 一致
    ok = asr_gpu_preflight(ARGV, run=smi(one_gpu(required)), which=WHICH)
    assert ok.ok and ok.required_mib == required and ok.free_mib == required
    short = asr_gpu_preflight(ARGV, run=smi(one_gpu(required - 1)), which=WHICH)
    assert short.kind == "insufficient" and f"需要 {required} MiB" in short.reason and f"空闲 {required - 1} MiB" in short.reason


def test_live_like_reading():
    result = asr_gpu_preflight(ARGV, run=smi(one_gpu(1519)), which=WHICH)
    assert result.kind == "insufficient" and result.total_mib == 16303


def test_missing_nvidia_smi_is_unknown_not_enough_and_distinguished_from_failure():
    calls: list = []
    result = asr_gpu_preflight(ARGV, run=smi("", calls=calls), which=lambda n: None, os_name="linux")
    assert result.kind == "unknown" and "未找到 nvidia-smi" in result.reason and calls == []

    with pytest.raises(NvidiaSmiNotFoundError):
        query_gpus(run=smi("", calls=calls), which=lambda n: None, os_name="linux", environ={})

    with pytest.raises(ValueError) as bad_rc:
        query_gpus(run=smi("", rc=9), which=WHICH, os_name="linux", environ={})
    assert not isinstance(bad_rc.value, NvidiaSmiNotFoundError)


@pytest.mark.parametrize(
    ("out", "rc", "needle"),
    [
        ("", 9, "退出码 9"),
        ("0, GPU-x, [N/A], [N/A], 572.16, 8.9, Some GPU\n", 0, "无法解析"),
        ("0, GPU-x, 16303, 12000, Old5ColName\n", 0, "无法解析"),
        ("garbage\n", 0, "无法解析"),
        ("\n", 0, "没有列出任何 GPU"),
    ],
)
def test_unparseable_or_failing_smi_is_unknown(out, rc, needle):
    result = asr_gpu_preflight(ARGV, run=smi(out, rc), which=WHICH)
    assert result.kind == "unknown" and needle in result.reason


def test_smi_timeout_is_unknown():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    assert asr_gpu_preflight(ARGV, run=run, which=WHICH).kind == "unknown"


def test_query_gpus_parses_seven_fields_including_commas_in_name():
    calls: list = []
    line = "0, GPU-comma, 12288, 10000, 572.16, 8.9, NVIDIA GeForce RTX 4070, Laptop GPU\n"
    gpus = query_gpus(run=smi(line, calls=calls), which=WHICH, os_name="linux", environ={})
    assert len(gpus) == 1
    gpu = gpus[0]
    assert (gpu.index, gpu.uuid, gpu.total_mib, gpu.free_mib, gpu.driver_version, gpu.compute_cap) == (
        "0", "GPU-comma", 12288, 10000, "572.16", "8.9",
    )
    assert gpu.name == "NVIDIA GeForce RTX 4070, Laptop GPU"
    assert calls[0][0] == "/usr/bin/nvidia-smi"
    assert calls[0][1] == "--query-gpu=index,uuid,memory.total,memory.free,driver_version,compute_cap,name"


def test_find_nvidia_smi_windows_fallback_order_and_case_insensitive_plain_dict(tmp_path):
    sysroot = tmp_path / "Windows"
    progfiles = tmp_path / "Program Files"
    sys32_smi = sysroot / "System32" / "nvidia-smi.exe"
    nvsmi_smi = progfiles / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
    nvsmi_smi.parent.mkdir(parents=True)
    nvsmi_smi.write_bytes(b"")

    # 普通全大写 dict（模拟 Windows 环境映射被转为普通 dict 的调用方）
    env_upper = {"SYSTEMROOT": str(sysroot), "PROGRAMFILES": str(progfiles)}
    assert find_nvidia_smi(which=lambda _n: None, os_name="windows", environ=env_upper) == str(nvsmi_smi)

    # System32 存在时优先于 NVSMI，并直接作为 cmd[0] 传给 subprocess.run
    sys32_smi.parent.mkdir(parents=True)
    sys32_smi.write_bytes(b"")
    assert find_nvidia_smi(which=lambda _n: None, os_name="windows", environ=env_upper) == str(sys32_smi)

    calls: list = []
    gpus = query_gpus(
        run=smi(one_gpu(10000), calls=calls, expected_cmd0=str(sys32_smi)),
        which=lambda _n: None,
        os_name="windows",
        environ=env_upper,
    )
    assert len(gpus) == 1 and calls[0][0] == str(sys32_smi)

    # which 命中时最优先
    assert find_nvidia_smi(which=lambda _n: r"C:\Custom\nvidia-smi.exe", os_name="windows", environ=env_upper) == r"C:\Custom\nvidia-smi.exe"

    # Linux 不走 Windows 目录兜底
    assert find_nvidia_smi(which=lambda _n: None, os_name="linux", environ=env_upper) is None


def test_multiple_gpus_without_clear_choice_is_unknown_without_vllm_wording():
    out = one_gpu(16000) + "1, GPU-abcd, 24564, 24000, 572.16, 8.6, NVIDIA RTX A5000\n"
    result = asr_gpu_preflight(ARGV, run=smi(out), which=WHICH, environ={})
    assert result.kind == "unknown" and "2 张 GPU" in result.reason and "vLLM" not in result.reason


TWO_GPUS = (
    "0, GPU-aaaa-1111, 16303, 1000, 572.16, 8.9, NVIDIA GeForce RTX 5070 Ti\n"
    "1, GPU-bbbb-2222, 24564, 20000, 572.16, 8.6, NVIDIA RTX A5000\n"
)


def test_cuda_visible_devices_single_index_and_uuid():
    by_index_ok = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": "1"})
    assert by_index_ok.ok and by_index_ok.gpu == "#1 NVIDIA RTX A5000" and by_index_ok.free_mib == 20000

    by_index_short = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": "0"})
    assert by_index_short.kind == "insufficient" and by_index_short.gpu == "#0 NVIDIA GeForce RTX 5070 Ti"

    by_uuid_exact = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": "GPU-bbbb-2222"})
    assert by_uuid_exact.ok and by_uuid_exact.gpu == "#1 NVIDIA RTX A5000"

    by_uuid_prefix = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": "GPU-b"})
    assert by_uuid_prefix.ok and by_uuid_prefix.gpu == "#1 NVIDIA RTX A5000"


@pytest.mark.parametrize("cvd", ["", "   ", ","])
def test_cuda_visible_devices_empty_rejects_even_on_single_gpu(cvd):
    single = asr_gpu_preflight(ARGV, run=smi(one_gpu(16000)), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": cvd})
    assert not single.ok and single.kind == "unknown"
    assert "CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU" in single.reason

    multi = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": cvd})
    assert not multi.ok and multi.kind == "unknown"
    assert "CUDA_VISIBLE_DEVICES 为空，未暴露任何 GPU" in multi.reason


@pytest.mark.parametrize(
    ("cvd", "needle"),
    [
        ("0,1", "多张 GPU"),
        ("9", "未匹配"),
        ("GPU-nonexistent", "未匹配"),
    ],
)
def test_cuda_visible_devices_multi_or_unknown_id_rejects(cvd, needle):
    result = asr_gpu_preflight(ARGV, run=smi(TWO_GPUS), which=WHICH, environ={"CUDA_VISIBLE_DEVICES": cvd})
    assert not result.ok and result.kind == "unknown" and needle in result.reason and "vLLM" not in result.reason


def test_windows_cuda_eligibility_and_preflight_matrix():
    good_gpus = query_gpus(run=smi(one_gpu(8000, total=8192, driver="570.00", compute_cap="7.0")), which=WHICH, os_name="windows", environ={})
    assert evaluate_windows_cuda_gpu(good_gpus, machine="AMD64", environ={}).status == "eligible"

    # 不支持硬件/显式隐藏 → unsupported_cpu（合法回 CPU，不砖化）
    assert evaluate_windows_cuda_gpu(good_gpus, machine="ARM64", environ={}).status == "unsupported_cpu"
    assert evaluate_windows_cuda_gpu(good_gpus, machine="AMD64", environ={"CUDA_VISIBLE_DEVICES": ""}).status == "unsupported_cpu"
    assert evaluate_windows_cuda_gpu(good_gpus, machine="AMD64", environ={"cuda_visible_devices": " , "}).status == "unsupported_cpu"

    small_vram = query_gpus(run=smi(one_gpu(4000, total=4096, driver="572.16", compute_cap="8.6")), which=WHICH, os_name="windows", environ={})
    assert evaluate_windows_cuda_gpu(small_vram, machine="AMD64", environ={}).status == "unsupported_cpu"

    pascal = query_gpus(run=smi(one_gpu(8000, total=8192, driver="572.16", compute_cap="6.1")), which=WHICH, os_name="windows", environ={})
    assert evaluate_windows_cuda_gpu(pascal, machine="AMD64", environ={}).status == "unsupported_cpu"

    # 硬件合格但驱动 < 570 或不可解析 → error（绝不静默降级 CPU）
    old_driver = query_gpus(run=smi(one_gpu(8000, total=8192, driver="566.36", compute_cap="8.9")), which=WHICH, os_name="windows", environ={})
    verdict_old = evaluate_windows_cuda_gpu(old_driver, machine="AMD64", environ={})
    assert verdict_old.status == "error" and "570" in verdict_old.reason

    bad_driver = query_gpus(run=smi(one_gpu(8000, total=8192, driver="[N/A]", compute_cap="8.9")), which=WHICH, os_name="windows", environ={})
    assert evaluate_windows_cuda_gpu(bad_driver, machine="AMD64", environ={}).status == "error"

    bad_cap = query_gpus(run=smi(one_gpu(8000, total=8192, driver="572.16", compute_cap="[N/A]")), which=WHICH, os_name="windows", environ={})
    assert evaluate_windows_cuda_gpu(bad_cap, machine="AMD64", environ={}).status == "error"

    # 多卡无法确定 → error；用全大写普通 dict 指定单卡 → eligible
    multi_gpus = query_gpus(run=smi(TWO_GPUS), which=WHICH, os_name="windows", environ={})
    verdict_multi = evaluate_windows_cuda_gpu(multi_gpus, machine="AMD64", environ={})
    assert verdict_multi.status == "error" and "vLLM" not in verdict_multi.reason
    verdict_picked = evaluate_windows_cuda_gpu(multi_gpus, machine="AMD64", environ={"CUDA_VISIBLE_DEVICES": "1"})
    assert verdict_picked.status == "eligible" and verdict_picked.gpu is not None and verdict_picked.gpu.index == "1"

    # 启动态预检校验空闲显存 >= 4608 MiB 与驱动 >= 570
    ok_pf = asr_windows_cuda_preflight(
        run=smi(one_gpu(MIN_WIN_CUDA_VRAM_MIB, total=8192, driver="572.16", compute_cap="8.9")),
        which=WHICH,
        environ={},
        machine="AMD64",
    )
    assert ok_pf.ok and ok_pf.required_mib == MIN_WIN_CUDA_VRAM_MIB and ok_pf.free_mib == MIN_WIN_CUDA_VRAM_MIB

    short_pf = asr_windows_cuda_preflight(
        run=smi(one_gpu(MIN_WIN_CUDA_VRAM_MIB - 1, total=8192, driver="572.16", compute_cap="8.9")),
        which=WHICH,
        environ={},
        machine="AMD64",
    )
    assert short_pf.kind == "insufficient" and "显存不足" in short_pf.reason

    old_pf = asr_windows_cuda_preflight(
        run=smi(one_gpu(8000, total=8192, driver="566.36", compute_cap="8.9")),
        which=WHICH,
        environ={},
        machine="AMD64",
    )
    assert old_pf.kind == "unknown" and "570" in old_pf.reason


def test_no_torch_or_vllm_imported():
    assert not [m for m in sys.modules if m.split(".")[0] in ("torch", "vllm")]


def test_query_gpus_passes_windows_subprocess_kwargs(monkeypatch):
    from _platform import child_process as cp_mod

    seen: list[dict] = []

    def spy_run(cmd, **kw):
        seen.append(dict(kw))
        return subprocess.CompletedProcess(cmd, 0, one_gpu(12000), "")

    monkeypatch.setattr(cp_mod.os, "name", "posix")
    assert len(query_gpus(run=spy_run, which=WHICH, os_name="linux", environ={})) == 1
    assert "creationflags" not in seen[-1] and seen[-1]["encoding"] == "utf-8" and seen[-1]["errors"] == "replace"

    monkeypatch.setattr(cp_mod.os, "name", "nt")
    assert len(query_gpus(run=spy_run, which=WHICH, os_name="linux", environ={})) == 1
    assert seen[-1]["creationflags"] == cp_mod.CREATE_NO_WINDOW
    assert seen[-1]["encoding"] == "utf-8" and seen[-1]["errors"] == "replace"


def test_find_nvidia_smi_uses_explicit_environ_path_without_leaking_global_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_smi = fake_bin / "nvidia-smi"
    fake_smi.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_smi.chmod(0o755)

    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    host_smi = host_bin / "nvidia-smi"
    host_smi.write_text("#!/bin/sh\n", encoding="utf-8")
    host_smi.chmod(0o755)
    monkeypatch.setenv("PATH", str(host_bin))

    # 1) 显式 environ["PATH"] 精准命中 fake_bin/nvidia-smi，绕开全局 os.environ["PATH"]
    assert find_nvidia_smi(os_name="linux", environ={"PATH": str(fake_bin)}) == str(fake_smi)

    # 2) 显式 environ={} 返回 None，绝不泄漏宿主机全局 os.environ["PATH"] 里的 nvidia-smi
    assert find_nvidia_smi(os_name="linux", environ={}) is None
    assert find_nvidia_smi(os_name="windows", environ={}) is None

    # 3) Windows 下大小写混合的 "Path" 正常解析；空 PATH 仍继续兜底显式 SystemRoot
    assert find_nvidia_smi(os_name="windows", environ={"Path": str(fake_bin)}) == str(fake_smi)
    sysroot = tmp_path / "WinRoot"
    sys32_smi = sysroot / "System32" / "nvidia-smi.exe"
    sys32_smi.parent.mkdir(parents=True)
    sys32_smi.write_bytes(b"MZ")
    assert find_nvidia_smi(os_name="windows", environ={"SYSTEMROOT": str(sysroot)}) == str(sys32_smi)
