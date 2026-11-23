"""U6 识别运行环境安装：结构化硬件探测与 30s 缓存、变体与锁文件映射、同卷磁盘预算扣减、
原子改名清理、cpu→cuda 安全升级与损坏清单重装、uv 发行包 sha256 校验、cancel_event 保护。假 HTTP 与假进程，不触网。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
import zipfile

import httpx
import pytest

from _contracts import ErrorCode, LogicError
from _paths import AppPaths
from _v3.runtime_install import (
    PYTHON_VERSION,
    REMOVING_VENV_PREFIX,
    UV_VERSION,
    AsrHardwareProbe,
    CachedHardwareProbe,
    asr_runtime_cleanup,
    asr_runtime_disk_budget,
    asr_runtime_install_action,
    choose_variant,
    detect_variant,
    lock_file,
    probe_asr_hardware,
    uv_asset,
    variant_disk_bytes,
)
from _v3.setup_tasks import GIB

ASSET = "uv-x86_64-unknown-linux-gnu.tar.gz"
WIN_ASSET = "uv-x86_64-pc-windows-msvc.zip"


def _uv_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _uv_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("uv.exe", b"MZ-fake-uv")
    return buf.getvalue()


def _release(blob: bytes, digest: str, asset: str = ASSET):
    def handler(request: httpx.Request) -> httpx.Response:
        assert f"/{UV_VERSION}/" in request.url.path
        if request.url.path.endswith(f"/{asset}.sha256"):
            return httpx.Response(200, text=f"{digest}  {asset}\n")
        if request.url.path.endswith(f"/{asset}"):
            return httpx.Response(200, content=blob)
        return httpx.Response(404)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


class Recorder:
    def __init__(self, fail_on: str | None = None, on_call=None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.kwargs: list[dict] = []
        self.fail_on = fail_on
        self.on_call = on_call

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw["env"]))
        self.kwargs.append(dict(kw))
        if self.on_call is not None:
            self.on_call(list(argv))
        failed = self.fail_on is not None and self.fail_on in argv
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "network unreachable" if failed else "")


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r", os_name="linux")


def _action(paths, run, client_factory, variant=("cpu", "未检测到 NVIDIA GPU"), **extra):
    return asr_runtime_install_action(
        paths=paths,
        os_name=extra.pop("os_name", "linux"),
        machine=extra.pop("machine", "x86_64"),
        variant=lambda: variant,
        environ={"PATH": "/usr/bin"},
        client_factory=client_factory,
        run=run,
        clock=lambda: "2026-11-20T00:00:00+00:00",
        **extra,
    )


def test_variant_choice_and_detection(tmp_path):
    assert choose_variant("linux", "x86_64", has_nvidia_gpu=True) == "cuda"
    assert choose_variant("windows", "AMD64", has_nvidia_gpu=True) == "cuda"
    assert choose_variant("windows", "ARM64", has_nvidia_gpu=True) == "cpu"
    assert choose_variant("macos", "arm64", has_nvidia_gpu=False) == "mps"
    assert choose_variant("linux", "x86_64", has_nvidia_gpu=False) == "cpu"

    variant, why = detect_variant("linux", "x86_64", run=subprocess.run, which=lambda name: None, environ={})
    assert variant == "cpu" and "未检测到 NVIDIA GPU" in why and "nvidia-smi" in why

    smi_7col = lambda cmd, **kw: subprocess.CompletedProcess(  # noqa: E731
        cmd, 0, "0, GPU-a, 16303, 15000, 572.16, 12.0, RTX 5070 Ti\n", ""
    )
    assert detect_variant("linux", "x86_64", run=smi_7col, which=lambda name: "/usr/bin/nvidia-smi", environ={}) == (
        "cuda",
        "检测到 GPU：RTX 5070 Ti",
    )

    # macOS 绝不调用 nvidia-smi
    mac = probe_asr_hardware(
        "macos",
        "arm64",
        run=lambda *a, **k: pytest.fail("macOS 不应调用 nvidia-smi"),
        which=lambda _n: pytest.fail("macOS 不应查找 nvidia-smi"),
        environ={},
    )
    assert mac.ok and mac.target_variant == "mps"

    # Windows 符合准入（经大写 SYSTEMROOT 兜底定位 nvidia-smi.exe）→ cuda
    sysroot = tmp_path / "Windows"
    smi_exe = sysroot / "System32" / "nvidia-smi.exe"
    smi_exe.parent.mkdir(parents=True)
    smi_exe.write_bytes(b"exe")
    seen_cmd: list[list[str]] = []

    def win_ok(cmd, **kw):
        seen_cmd.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "0, GPU-w, 12288, 10000, 572.16, 8.9, RTX 4070\n", "")

    win_probe = probe_asr_hardware(
        "windows",
        "AMD64",
        run=win_ok,
        which=lambda _n: None,
        environ={"SYSTEMROOT": str(sysroot)},
    )
    assert win_probe.ok and win_probe.target_variant == "cuda" and win_probe.gpu_name == "RTX 4070"
    assert seen_cmd[0][0] == str(smi_exe)

    # Windows 旧驱动 < 570 或 nvidia-smi 故障：显式报错，绝不静默降级 CPU
    old_drv = lambda cmd, **kw: subprocess.CompletedProcess(  # noqa: E731
        cmd, 0, "0, GPU-w, 12288, 10000, 566.36, 8.9, RTX 4070\n", ""
    )
    bad_drv = probe_asr_hardware("windows", "AMD64", run=old_drv, which=lambda _n: str(smi_exe), environ={})
    assert not bad_drv.ok and bad_drv.target_variant is None and bad_drv.error is not None and "570" in bad_drv.error
    with pytest.raises(LogicError) as drv_err:
        detect_variant("windows", "AMD64", run=old_drv, which=lambda _n: str(smi_exe), environ={})
    assert drv_err.value.code == ErrorCode.dependency_failed and "570" in drv_err.value.message

    broken = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 9, "", "NVML failed")  # noqa: E731
    broken_probe = probe_asr_hardware("windows", "AMD64", run=broken, which=lambda _n: str(smi_exe), environ={})
    assert not broken_probe.ok and broken_probe.target_variant is None and "NVML failed" in (broken_probe.error or "")
    with pytest.raises(LogicError) as broken_err:
        detect_variant("windows", "AMD64", run=broken, which=lambda _n: str(smi_exe), environ={})
    assert broken_err.value.code == ErrorCode.dependency_failed

    # Windows 硬件不合格（Pascal 6.1 / 显存不足 / 无 nvidia-smi / CUDA_VISIBLE_DEVICES=""）合法返回 cpu
    pascal = lambda cmd, **kw: subprocess.CompletedProcess(  # noqa: E731
        cmd, 0, "0, GPU-p, 8192, 7000, 572.16, 6.1, GTX 1070\n", ""
    )
    assert probe_asr_hardware("windows", "AMD64", run=pascal, which=lambda _n: str(smi_exe), environ={}).target_variant == "cpu"
    assert probe_asr_hardware("windows", "AMD64", run=win_ok, which=lambda _n: None, environ={}).target_variant == "cpu"
    assert (
        probe_asr_hardware(
            "windows",
            "AMD64",
            run=broken,
            which=lambda _n: str(smi_exe),
            environ={"CUDA_VISIBLE_DEVICES": ""},
        ).target_variant
        == "cpu"
    )

    assert uv_asset("windows", "AMD64") == "uv-x86_64-pc-windows-msvc.zip" and uv_asset("macos", "arm64").endswith("apple-darwin.tar.gz")


def test_cached_hardware_probe_caches_success_and_error_and_supports_invalidate():
    clock = {"now": 100.0}
    responses = [
        AsrHardwareProbe(target_variant=None, detail="驱动旧", error="驱动低于 570"),
        AsrHardwareProbe(target_variant="cuda", detail="RTX 4070"),
        AsrHardwareProbe(target_variant="cpu", detail="无卡"),
    ]
    calls = {"n": 0}

    def raw_probe() -> AsrHardwareProbe:
        idx = calls["n"]
        calls["n"] += 1
        return responses[idx]

    cache = CachedHardwareProbe(raw_probe, ttl_s=30.0, monotonic=lambda: clock["now"])
    first = cache.get()
    assert first.error == "驱动低于 570" and calls["n"] == 1
    clock["now"] += 10.0
    assert cache.get() is first and calls["n"] == 1  # 失败结果同样缓存 30s

    cache.invalidate()
    second = cache.get()
    assert second.target_variant == "cuda" and calls["n"] == 2

    clock["now"] += 30.0  # TTL 到期重探
    third = cache.get()
    assert third.target_variant == "cpu" and calls["n"] == 3


def test_variant_disk_budget_deducts_same_volume_reclaimable_and_clamps_zero(paths):
    assert variant_disk_bytes("linux", "cuda") == 16 * GIB
    assert variant_disk_bytes("windows", "cuda") == 12 * GIB
    assert variant_disk_bytes("windows", "cpu") == 4 * GIB
    assert variant_disk_bytes("macos", "mps") == 4 * GIB

    paths.asr_venv.mkdir(parents=True)
    (paths.asr_venv / "old.bin").write_bytes(b"a" * 1024)
    leftover = paths.asr_venv.parent / f"{REMOVING_VENV_PREFIX}999.deadbeef"
    leftover.mkdir(parents=True)
    (leftover / "left.bin").write_bytes(b"b" * 2048)
    uv_cache = paths.uv_dir / "cache"
    uv_cache.mkdir(parents=True)
    (uv_cache / "wheel.bin").write_bytes(b"c" * 4096)

    target, need = asr_runtime_disk_budget(paths, "windows", "cuda")
    assert target == paths.asr_venv
    assert need == 12 * GIB - (1024 + 2048 + 4096)

    # 极端大扣减：钳制到 >= 0（由 disk_shortfall 保证至少 1 GiB 余量）
    huge = paths.asr_venv / "huge.bin"
    with open(huge, "wb") as fh:
        fh.truncate(5 * GIB)
    _target, clamped = asr_runtime_disk_budget(paths, "windows", "cpu")
    assert clamped == 0


def test_installs_uv_python_venv_and_hashed_lock_then_writes_manifest_last(paths):
    blob = _uv_tarball()
    run = Recorder()
    progress = []
    message = _action(paths, run, _release(blob, hashlib.sha256(blob).hexdigest()))(progress.append)
    uv = str(paths.uv_dir / "uv")
    assert [argv for argv, _env in run.calls] == [
        [uv, "python", "install", PYTHON_VERSION],
        [uv, "venv", "--clear", "--python", PYTHON_VERSION, str(paths.asr_venv)],
        [uv, "pip", "install", "--python", str(paths.asr_python), "--require-hashes", "-r", str(lock_file("cpu"))],
        [uv, "cache", "clean"],
    ]
    env = run.calls[0][1]
    assert env["UV_PYTHON_INSTALL_DIR"] == str(paths.python_dir) and env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed" and env["PYTHONUTF8"] == "1"
    assert run.kwargs[0]["encoding"] == "utf-8" and run.kwargs[0]["errors"] == "replace"
    manifest = json.loads(paths.asr_manifest.read_text(encoding="utf-8"))
    assert manifest == {"variant": "cpu", "python": PYTHON_VERSION, "uv": UV_VERSION,
                        "lock_sha256": hashlib.sha256(lock_file("cpu").read_bytes()).hexdigest(),
                        "installed_at": "2026-11-20T00:00:00+00:00"}
    assert "cpu" in message and progress[0].stage == "识别运行环境变体 cpu：未检测到 NVIDIA GPU"
    assert progress[1].stage.startswith("下载 uv") and progress[-1].stage == "清理下载缓存"
    assert not list(paths.uv_dir.glob(".*"))  # 下载与解压临时文件已删
    with pytest.raises(LogicError) as dup:  # 同变体已安装不重复安装
        _action(paths, run, _release(blob, hashlib.sha256(blob).hexdigest()))(progress.append)
    assert dup.value.code == ErrorCode.conflict


def test_upgrade_cpu_to_cuda_and_corrupted_manifest_preserves_old_env_until_replace(tmp_path, monkeypatch):
    win_paths = AppPaths.for_roots(
        config_root=tmp_path / "c",
        data_root=tmp_path / "d",
        runtime_dir=tmp_path / "r",
        os_name="windows",
        environ={"APPDATA": str(tmp_path / "live-app"), "LOCALAPPDATA": str(tmp_path / "live-local")},
    )
    win_paths.asr_venv.mkdir(parents=True)
    (win_paths.asr_venv / "marker.txt").write_text("old-cpu", encoding="utf-8")
    win_paths.asr_manifest.parent.mkdir(parents=True, exist_ok=True)
    old_manifest = json.dumps({"variant": "cpu"})
    win_paths.asr_manifest.write_text(old_manifest, encoding="utf-8")
    # 预先放一个历史残留 .venv.removing.* 验证一并清扫
    stale = win_paths.asr_venv.parent / f"{REMOVING_VENV_PREFIX}77.abcdef01"
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x", encoding="utf-8")

    blob = _uv_zip()
    digest = hashlib.sha256(blob).hexdigest()

    # 阶段 A：下载 uv 或安装 Python 失败 → on_replace_start 不触发，旧 venv 与 manifest 完整保留
    replaced = threading.Event()
    fail_py = Recorder(fail_on="python")
    with pytest.raises(LogicError):
        _action(
            win_paths,
            fail_py,
            _release(blob, digest, asset=WIN_ASSET),
            variant=("cuda", "RTX 4070"),
            os_name="windows",
            machine="AMD64",
            on_replace_start=replaced.set,
        )(lambda _p: None)
    assert not replaced.is_set()
    assert win_paths.asr_manifest.read_text(encoding="utf-8") == old_manifest
    assert (win_paths.asr_venv / "marker.txt").read_text(encoding="utf-8") == "old-cpu"

    # 阶段 B：清理时目录被进程锁死（rename 抛 PermissionError）→ 包装为 LogicError(dependency_failed) 且不删旧 manifest
    monkeypatch.setattr(os, "rename", lambda _src, _dst: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(LogicError) as locked_err:
        _action(
            win_paths,
            Recorder(),
            _release(blob, digest, asset=WIN_ASSET),
            variant=("cuda", "RTX 4070"),
            os_name="windows",
            machine="AMD64",
        )(lambda _p: None)
    assert locked_err.value.code == ErrorCode.dependency_failed and "占用" in locked_err.value.message
    assert win_paths.asr_manifest.read_text(encoding="utf-8") == old_manifest
    monkeypatch.undo()

    # 阶段 C：正常 cpu → cuda 升级：触发 on_replace_start、清扫旧 venv 与历史 .venv.removing.*、用 requirements-cuda-win.lock、写 cuda 清单
    events: list[str] = []

    def track_call(argv: list[str]) -> None:
        if "python" in argv and "install" in argv:
            assert not replaced.is_set() and win_paths.asr_manifest.is_file()
            events.append("python_install")
        elif "venv" in argv:
            assert replaced.is_set() and not win_paths.asr_manifest.exists() and not stale.exists()
            events.append("venv")

    run_ok = Recorder(on_call=track_call)
    msg = _action(
        win_paths,
        run_ok,
        _release(blob, digest, asset=WIN_ASSET),
        variant=("cuda", "RTX 4070"),
        os_name="windows",
        machine="AMD64",
        on_replace_start=replaced.set,
    )(lambda _p: None)
    assert events == ["python_install", "venv"]
    pip_argv = next(argv for argv, _env in run_ok.calls if "pip" in argv)
    assert pip_argv[-1] == str(lock_file("cuda", os_name="windows"))
    assert json.loads(win_paths.asr_manifest.read_text(encoding="utf-8"))["variant"] == "cuda"
    assert "cuda" in msg and not stale.exists()

    # 阶段 D：清单损坏允许重装；cancel_event 在写清单前被置位时绝不写 manifest.json
    win_paths.asr_manifest.write_text("{corrupt", encoding="utf-8")
    cancel = threading.Event()
    run_cancel = Recorder(on_call=lambda argv: cancel.set() if "cache" in argv else None)
    with pytest.raises(LogicError) as cancelled:
        _action(
            win_paths,
            run_cancel,
            _release(blob, digest, asset=WIN_ASSET),
            variant=("cuda", "RTX 4070"),
            os_name="windows",
            machine="AMD64",
            cancel_event=cancel,
        )(lambda _p: None)
    assert cancelled.value.code == ErrorCode.dependency_failed and not win_paths.asr_manifest.exists()


def test_checksum_mismatch_or_failed_step_never_writes_manifest_and_cleanup_resets(paths, monkeypatch):
    blob = _uv_tarball()
    run = Recorder()
    with pytest.raises(LogicError) as bad:
        _action(paths, run, _release(blob, "0" * 64))(lambda p: None)
    assert "sha256" in bad.value.message and run.calls == [] and not (paths.uv_dir / "uv").exists()

    failing = Recorder(fail_on="--require-hashes")
    with pytest.raises(LogicError) as failed:
        _action(paths, failing, _release(blob, hashlib.sha256(blob).hexdigest()))(lambda p: None)
    assert "安装识别依赖（cpu）失败（退出码 1）：network unreachable" == failed.value.message
    assert not paths.asr_manifest.exists()

    paths.asr_venv.mkdir(parents=True)
    paths.asr_manifest.write_text("{}", encoding="utf-8")
    stale_dir = paths.asr_venv.parent / f"{REMOVING_VENV_PREFIX}42.01234567"
    stale_dir.mkdir(parents=True)
    asr_runtime_cleanup(paths)
    assert not paths.asr_venv.exists() and not paths.asr_manifest.exists() and not stale_dir.exists()
    assert (paths.uv_dir / "uv").is_file()

    # rename 持续 PermissionError：抛出 PermissionError 且保留 asr_manifest 不删
    paths.asr_venv.mkdir(parents=True)
    paths.asr_manifest.write_text('{"variant":"cpu"}', encoding="utf-8")
    monkeypatch.setattr(os, "rename", lambda _s, _d: (_ for _ in ()).throw(PermissionError("busy")))
    with pytest.raises(PermissionError):
        asr_runtime_cleanup(paths, rename_retries=2, retry_delay_s=0.0)
    assert paths.asr_manifest.is_file() and paths.asr_venv.is_dir()


@pytest.mark.parametrize(
    ("variant", "os_name"),
    [("cuda", "linux"), ("cuda", "windows"), ("cpu", "linux"), ("mps", "macos")],
)
def test_each_variant_has_a_hash_locked_requirements_file(variant, os_name):
    text = lock_file(variant, os_name=os_name).read_text(encoding="utf-8")
    assert "--hash=sha256:" in text and "qwen-asr==0.0.6" in text and "--index-url https://pypi.org/simple" in text
    assert ("vllm==0.14.0" in text) is (variant == "cuda" and os_name == "linux")
    assert ("torch-2.9.1%2Bcu128-cp312-cp312-win_amd64.whl" in text) is (variant == "cuda" and os_name == "windows")
    assert ("torch-2.9.1%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl" in text) is (variant == "cpu")
    if variant == "cuda" and os_name == "windows":
        assert "--extra-index-url" not in text


def test_lock_file_uses_isolated_asr_runtime_root_when_frozen(tmp_path):
    assert lock_file("cpu", program_root=tmp_path, frozen=False) == tmp_path / "runtimes" / "asr" / "requirements-cpu.lock"
    assert lock_file("cpu", program_root=tmp_path, frozen=True) == (
        tmp_path / "_asr_runtime" / "runtimes" / "asr" / "requirements-cpu.lock"
    )
    assert lock_file("cuda", os_name="windows", program_root=tmp_path, frozen=True) == (
        tmp_path / "_asr_runtime" / "runtimes" / "asr" / "requirements-cuda-win.lock"
    )


def test_run_uv_passes_windows_no_window_and_utf8_on_nt(paths, monkeypatch):
    from _platform import child_process as cp_mod

    blob = _uv_tarball()
    run = Recorder()
    monkeypatch.setattr(cp_mod, "_is_nt", lambda: True)
    _action(paths, run, _release(blob, hashlib.sha256(blob).hexdigest()))(lambda _p: None)
    assert all(kw["creationflags"] == cp_mod.CREATE_NO_WINDOW for kw in run.kwargs)
    assert all(kw["encoding"] == "utf-8" and kw["errors"] == "replace" for kw in run.kwargs)
    assert all(env["PYTHONUTF8"] == "1" for _argv, env in run.calls)
