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
    INSTALL_TIMEOUT_S,
    PYTHON_VERSION,
    REMOVING_VENV_PREFIX,
    STALL_TIMEOUT_S,
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
from _v3.uv_runner import UvStreamTracker, _scan_path, dir_bytes, run_uv_streaming, strip_ansi

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


class _FakeProc:
    def __init__(self, returncode: int, output: str, *, pid: int = 42001) -> None:
        self.pid = pid
        self.stdout = io.StringIO(output)
        self.returncode: int | None = None
        self._final_rc = returncode
        self.killed = False
        self.terminated = False

    def poll(self) -> int | None:
        self.returncode = self._final_rc
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._final_rc
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class Recorder:
    def __init__(self, fail_on: str | None = None, on_call=None, output: str = "") -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.kwargs: list[dict] = []
        self.fail_on = fail_on
        self.on_call = on_call
        self.output = output

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), dict(kw["env"])))
        self.kwargs.append(dict(kw))
        if self.on_call is not None:
            self.on_call(list(argv))
        failed = self.fail_on is not None and self.fail_on in argv
        text = "error: network unreachable\n" if failed else self.output
        return _FakeProc(1 if failed else 0, text)


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r", os_name="linux")


def _action(paths, popen, client_factory, variant=("cpu", "未检测到 NVIDIA GPU"), **extra):
    return asr_runtime_install_action(
        paths=paths,
        os_name=extra.pop("os_name", "linux"),
        machine=extra.pop("machine", "x86_64"),
        variant=lambda: variant,
        environ=extra.pop("environ", {"PATH": "/usr/bin"}),
        client_factory=client_factory,
        popen=popen,
        read_system_proxy=extra.pop("read_system_proxy", lambda _os: ({}, [])),
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
        [uv, "python", "install", "--color", "never", PYTHON_VERSION],
        [uv, "venv", "--color", "never", "--clear", "--python", PYTHON_VERSION, str(paths.asr_venv)],
        [uv, "pip", "install", "--color", "never", "--python", str(paths.asr_python), "--require-hashes", "-r", str(lock_file("cpu"))],
        [uv, "cache", "clean", "--color", "never"],
    ]
    env = run.calls[0][1]
    assert env["UV_PYTHON_INSTALL_DIR"] == str(paths.python_dir) and env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed" and env["PYTHONUTF8"] == "1"
    assert env["NO_COLOR"] == "1" and env["UV_SYSTEM_CERTS"] == "1"
    assert env["UV_CONCURRENT_DOWNLOADS"] == "8" and env["UV_HTTP_TIMEOUT"] == "120"
    kw0 = run.kwargs[0]
    assert kw0["encoding"] == "utf-8" and kw0["errors"] == "replace"
    assert kw0["stdin"] == subprocess.DEVNULL and kw0["stdout"] == subprocess.PIPE and kw0["stderr"] == subprocess.STDOUT
    assert kw0["text"] is True and kw0["start_new_session"] is True
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
    assert "安装识别依赖（cpu）失败（退出码 1）" in failed.value.message
    assert "network unreachable" in failed.value.message
    assert str(paths.logs / "setup-asr_runtime_install.log") in failed.value.message
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
    assert all("start_new_session" not in kw for kw in run.kwargs)
    assert all(kw["encoding"] == "utf-8" and kw["errors"] == "replace" for kw in run.kwargs)
    assert all(env["PYTHONUTF8"] == "1" for _argv, env in run.calls)


def test_uv_env_merges_system_proxy_respects_user_overrides_and_lazy_client_factory(tmp_path):
    win_paths = AppPaths.for_roots(
        config_root=tmp_path / "c",
        data_root=tmp_path / "d",
        runtime_dir=tmp_path / "r",
        os_name="windows",
        environ={"APPDATA": str(tmp_path / "app"), "LOCALAPPDATA": str(tmp_path / "loc")},
    )
    # 预置已有 uv.exe：懒判定不触发 client_factory（即使 client_factory 抛 SOCKS5 错误也不阻断重试）
    win_paths.uv_dir.mkdir(parents=True)
    (win_paths.uv_dir / "uv.exe").write_bytes(b"MZ-existing-uv")

    rec = Recorder()
    called_client = {"n": 0}

    def forbidden_client():
        called_client["n"] += 1
        raise AssertionError("已有 uv.exe 时不得调用 client_factory")

    _action(
        win_paths,
        rec,
        forbidden_client,
        variant=("cuda", "RTX 4070"),
        os_name="windows",
        machine="AMD64",
        environ={
            "PATH": "C:\\Windows",
            "uv_system_certs": "0",
            "Uv_Concurrent_Downloads": "false",
            "uv_http_timeout": "   ",
            "uv_no_config": "0",
            "force_color": "1",
            "CLICOLOR_FORCE": "1",
        },
        read_system_proxy=lambda _os: (
            {"http": "http://alice:secret@127.0.0.1:7890", "https": "http://alice:secret@127.0.0.1:7890"},
            ["*.corp"],
        ),
    )(lambda _p: None)

    assert called_client["n"] == 0
    env = rec.calls[0][1]
    assert env["HTTP_PROXY"] == "http://alice:secret@127.0.0.1:7890"
    assert env["HTTPS_PROXY"] == "http://alice:secret@127.0.0.1:7890"
    assert env["NO_PROXY"] == "*.corp,127.0.0.1,localhost,::1"
    assert env["UV_SYSTEM_CERTS"] == "0" and "uv_system_certs" not in env
    assert env["UV_CONCURRENT_DOWNLOADS"] == "false" and "Uv_Concurrent_Downloads" not in env
    assert env["UV_HTTP_TIMEOUT"] == "120" and "uv_http_timeout" not in env
    assert env["UV_NO_CONFIG"] == "1" and "uv_no_config" not in env
    assert env["NO_COLOR"] == "1"
    assert all(k.upper() not in ("FORCE_COLOR", "CLICOLOR_FORCE") for k in env)


def test_uv_stream_tracker_parses_inflight_milestones_ansi_and_redaction():
    assert strip_ansi("\x1b[32mhello\x1b[0m\x1b]0;title\x07") == "hello"

    tracker = UvStreamTracker("安装识别依赖（cuda）")
    cleaned_dl = tracker.feed_line("\x1b[1mDownloading torch (2.7GiB)\x1b[0m\n")
    assert cleaned_dl == "Downloading torch (2.7GiB)"
    tracker.feed_line("Downloading cpython-3.12.13-windows-x86_64-none (download) (21.5MiB)\n")
    tracker.feed_line("Downloading numpy (16.4 MiB)\n")
    tracker.feed_line("Downloaded numpy\n")
    # 小包完成绝不覆盖仍在下载的最大包 torch；速率 <0.1 MB/s 不展示，>=0.1 MB/s 展示
    assert tracker.format_stage(0.05 * 1024 * 1024) == "安装识别依赖（cuda） · 下载 torch (2.7GiB)"
    assert tracker.format_stage(12.34 * 1024 * 1024) == "安装识别依赖（cuda） · 下载 torch (2.7GiB) · 12.3 MB/s"

    # 同体积并列优先最早入表
    tracker.feed_line("Downloaded torch\n")
    tracker.feed_line("Downloaded cpython-3.12.13-windows-x86_64-none (download)\n")
    tracker.feed_line("Downloading pkg-a (10.0MiB)\n")
    tracker.feed_line("Downloading pkg-b (10.0MiB)\n")
    assert "下载 pkg-a (10.0MiB)" in tracker.format_stage()

    # Downloaded N packages / Prepared / Installed 清空在途表并更新里程碑
    tracker.feed_line("Downloaded 62 packages in 4.2s\n")
    assert tracker.format_stage() == "安装识别依赖（cuda） · 已下载 62 个包"
    tracker.feed_line("Downloading stray (1.0MiB)\n")
    tracker.feed_line("Prepared 62 packages in 9.1s\n")
    assert tracker.format_stage() == "安装识别依赖（cuda） · 已准备 62 个包"
    tracker.feed_line("Installed 62 packages in 3.0s\n")
    assert tracker.format_stage() == "安装识别依赖（cuda） · 已安装 62 个包"

    # 报错链保留顶层 error: 与后续 Caused by:，并脱敏 URL 凭证
    bad_line = tracker.feed_line("error: Failed to download https://alice:topsecret@pypi.org/simple/torch/\n")
    tracker.feed_line("  Caused by: proxyconnect tcp http://bob:p@ss@127.0.0.1:7890: EOF\n")
    assert "topsecret" not in bad_line and "https://***@pypi.org/simple/torch/" in bad_line
    summary = tracker.error_summary()
    assert summary.startswith("error: Failed to download") and "Caused by:" in summary
    assert "topsecret" not in summary and "p@ss" not in summary and "http://***@127.0.0.1:7890" in summary


def test_dir_bytes_and_scan_path_handles_files_symlinks_and_oserror(tmp_path, monkeypatch):
    assert _scan_path(tmp_path / "missing") == (0, 0)
    single = tmp_path / "one.bin"
    single.write_bytes(b"x" * 17)
    assert _scan_path(single) == (17, 1) and dir_bytes(single) == 17

    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a" * 100)
    (root / "sub" / "b.bin").write_bytes(b"b" * 250)
    link = root / "link.bin"
    link.symlink_to(single)
    total, count = _scan_path(root)
    assert count == 3 and total == 100 + 250 + os.lstat(link).st_size

    # 并发写入时单个条目 stat 抛 FileNotFoundError 不中断扫描
    real_scandir = os.scandir

    class _FlakyEntry:
        def __init__(self, inner, boom: bool) -> None:
            self._inner = inner
            self._boom = boom
            self.path = inner.path

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return self._inner.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks: bool = True):
            if self._boom:
                raise FileNotFoundError("vanished .tmp")
            return self._inner.stat(follow_symlinks=follow_symlinks)

    class _FlakyCtx:
        def __init__(self, path):
            self._cm = real_scandir(path)

        def __enter__(self):
            entries = list(self._cm.__enter__())
            return [_FlakyEntry(e, e.name == "a.bin") for e in entries]

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(os, "scandir", lambda p: _FlakyCtx(p))
    flaky_bytes, flaky_count = _scan_path(root)
    assert flaky_bytes == 250 + os.lstat(link).st_size and flaky_count == 2


def test_run_uv_streaming_tracks_monotonic_bytes_rate_activity_and_stderr_resilience(tmp_path, monkeypatch):
    uv_cache = tmp_path / "uv_cache"
    asr_venv = tmp_path / "asr_venv"
    uv_cache.mkdir()
    asr_venv.mkdir()
    # 重试场景基线：启动前缓存已有 2 MiB，不应算出假速率
    (uv_cache / "existing.whl").write_bytes(b"0" * (2 * 1024 * 1024))

    r_fd, w_fd = os.pipe()
    reader_stream = os.fdopen(r_fd, "r", encoding="utf-8")
    writer_stream = os.fdopen(w_fd, "w", encoding="utf-8")

    clock = {"t": 100.0}
    step = {"i": 0}

    class _SteppingProc:
        pid = 51001
        stdout = reader_stream
        returncode: int | None = None

        def poll(self):
            step["i"] += 1
            if step["i"] == 1:
                writer_stream.write("\x1b[36mDownloading torch (2.7GiB)\x1b[0m\n")
                writer_stream.write("fetch http://u:secret@127.0.0.1:7890/torch.whl\n")
                writer_stream.flush()
                return None
            if step["i"] == 2:
                clock["t"] = 102.0
                (uv_cache / "chunk.tmp").write_bytes(b"1" * (4 * 1024 * 1024))
                return None
            if step["i"] == 3:
                # 缓存 .tmp 收缩（-1 MiB）但 asr_venv 写入 10 MiB：UI completed_bytes 单调保 6 MiB 不双计，且活动签名刷新防停滞
                clock["t"] = 104.0
                (uv_cache / "chunk.tmp").write_bytes(b"1" * (3 * 1024 * 1024))
                (asr_venv / "torch.pyd").write_bytes(b"2" * (10 * 1024 * 1024))
                return None
            if step["i"] == 4:
                # 跳到 112.0s（距上次 104.0s 仅 8s < stall=10s）仍不触发停滞，随后正常退出
                clock["t"] = 112.0
                writer_stream.close()
                self.returncode = 0
                return 0
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        def terminate(self):
            pass

    captured_err: list[str] = []

    class _TeeCapture:
        def write(self, data: str) -> int:
            captured_err.append(data)
            return len(data)

        def flush(self) -> None:
            return None

    monkeypatch.setattr("sys.stderr", _TeeCapture())
    progress: list = []
    run_uv_streaming(
        ["uv", "pip", "install"],
        env={"HTTPS_PROXY": "http://u:secret@127.0.0.1:7890"},
        what="安装识别依赖（cuda）",
        report=progress.append,
        os_name="windows",
        log_path=tmp_path / "setup-asr_runtime_install.log",
        primary_dir=uv_cache,
        activity_dirs=(uv_cache, asr_venv),
        popen=lambda *a, **kw: _SteppingProc(),
        monotonic=lambda: clock["t"],
        poll_interval_s=0.0,
        sample_interval_s=2.0,
        stall_timeout_s=10.0,
        rate_window_s=8.0,
    )
    assert progress[0].completed_bytes == 2 * 1024 * 1024 and "MB/s" not in (progress[0].stage or "")
    assert any(p.completed_bytes == 6 * 1024 * 1024 for p in progress)
    assert all((p.completed_bytes or 0) <= 6 * 1024 * 1024 for p in progress)  # 绝不累加 10 MiB 的 asr_venv
    assert any("MB/s" in (p.stage or "") for p in progress)
    err_joined = "".join(captured_err)
    assert "Downloading torch (2.7GiB)" in err_joined and "\x1b[" not in err_joined
    assert "http://***@127.0.0.1:7890/torch.whl" in err_joined and "secret" not in err_joined

    # sys.stderr 写抛 OSError 或为 None 时绝不中断管道消费
    class _BrokenStderr:
        def write(self, _data: str) -> int:
            raise OSError("broken pipe")

        def flush(self) -> None:
            raise OSError("broken pipe")

    for bad_stderr in (_BrokenStderr(), None):
        monkeypatch.setattr("sys.stderr", bad_stderr)
        drained: list[bool] = []

        class _DrainCheckProc(_FakeProc):
            def __init__(self):
                super().__init__(0, "line1\nline2\n")

            def poll(self):
                drained.append(True)
                self.returncode = 0
                return 0

        run_uv_streaming(
            ["uv", "cache", "clean"],
            env={},
            what="清理下载缓存",
            report=lambda _p: None,
            os_name="linux",
            log_path=tmp_path / "log.txt",
            popen=lambda *a, **kw: _DrainCheckProc(),
            poll_interval_s=0.0,
        )
        assert drained


def test_run_uv_streaming_stall_total_timeout_cancel_and_popen_oserror(tmp_path, monkeypatch):
    from _platform import _child_windows, child_process as cp_mod

    assert STALL_TIMEOUT_S == 300.0 and INSTALL_TIMEOUT_S == 3600.0
    log_path = tmp_path / "setup-asr_runtime_install.log"

    # 1. 停滞熔断（Windows 路径）：先调 terminate_descendants(pid) 再 kill()，报错含脱敏代理 host:port 与日志路径
    win_descendants: list[int] = []
    monkeypatch.setattr(cp_mod, "_is_nt", lambda: True)
    monkeypatch.setattr(_child_windows, "terminate_descendants", lambda pid, **kw: win_descendants.append(pid) or [])

    class _HangingProc:
        def __init__(self, pid: int = 77001) -> None:
            self.pid = pid
            self.stdout = io.StringIO("")
            self.returncode: int | None = None
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode or 0

    stall_clock = {"t": 0.0}

    def tick_stall() -> float:
        val = stall_clock["t"]
        stall_clock["t"] += 5.0
        return val

    hang = _HangingProc(pid=77001)
    with pytest.raises(LogicError) as stalled:
        run_uv_streaming(
            ["uv", "pip", "install"],
            env={"HTTPS_PROXY": "http://alice:topsecret@127.0.0.1:7890"},
            what="安装识别依赖（cuda）",
            report=lambda _p: None,
            os_name="windows",
            log_path=log_path,
            popen=lambda *a, **kw: hang,
            monotonic=tick_stall,
            poll_interval_s=0.0,
            sample_interval_s=2.0,
            stall_timeout_s=10.0,
            total_timeout_s=100.0,
        )
    assert stalled.value.code == ErrorCode.dependency_failed
    assert "疑似停滞" in stalled.value.message and "127.0.0.1:7890" in stalled.value.message
    assert "topsecret" not in stalled.value.message and str(log_path) in stalled.value.message
    assert win_descendants == [77001] and hang.killed is True

    # 2. 总超时（POSIX 路径）：有持续输出不触发停滞，但超过 total_timeout_s 后调 os.killpg 终止
    monkeypatch.setattr(cp_mod, "_is_nt", lambda: False)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killpg_calls.append((pid, int(sig))))

    r_fd, w_fd = os.pipe()
    r_stream = os.fdopen(r_fd, "r", encoding="utf-8")
    w_stream = os.fdopen(w_fd, "w", encoding="utf-8")
    tot_clock = {"t": 0.0}

    class _ChattyProc(_HangingProc):
        def __init__(self):
            super().__init__(pid=77002)
            self.stdout = r_stream

        def poll(self):
            if self.returncode is None:
                try:
                    w_stream.write("still working\n")
                    w_stream.flush()
                except ValueError:
                    pass
                tot_clock["t"] += 6.0
            return self.returncode

        def wait(self, timeout=None):
            if timeout is not None and timeout <= 1.0:
                raise subprocess.TimeoutExpired("uv", timeout)
            self.returncode = -9
            return -9

    chatty = _ChattyProc()
    try:
        with pytest.raises(LogicError) as timed_out:
            run_uv_streaming(
                ["uv", "pip", "install"],
                env={},
                what="安装识别依赖（cuda）",
                report=lambda _p: None,
                os_name="linux",
                log_path=log_path,
                popen=lambda *a, **kw: chatty,
                monotonic=lambda: tot_clock["t"],
                poll_interval_s=0.0,
                sample_interval_s=2.0,
                stall_timeout_s=30.0,
                total_timeout_s=15.0,
                grace_s=0.0,
            )
    finally:
        w_stream.close()
    assert "超时" in timed_out.value.message and str(log_path) in timed_out.value.message
    assert any(pid == 77002 for pid, _sig in killpg_calls)

    # 3. 取消路径：启动前取消不调 popen；运行中取消立即终止；退出后发现已取消同样拒绝成功
    pre_cancel = threading.Event()
    pre_cancel.set()
    with pytest.raises(LogicError) as c1:
        run_uv_streaming(
            ["uv", "pip", "install"],
            env={},
            what="安装识别依赖（cuda）",
            report=lambda _p: None,
            os_name="linux",
            log_path=log_path,
            popen=lambda *a, **kw: pytest.fail("已取消不得启动子进程"),
            cancel_event=pre_cancel,
        )
    assert "已中止" in c1.value.message

    mid_cancel = threading.Event()
    mid_proc = _HangingProc(pid=77003)

    def poll_and_cancel():
        mid_cancel.set()
        return None

    mid_proc.poll = poll_and_cancel  # type: ignore[method-assign]
    with pytest.raises(LogicError) as c2:
        run_uv_streaming(
            ["uv", "pip", "install"],
            env={},
            what="安装识别依赖（cuda）",
            report=lambda _p: None,
            os_name="linux",
            log_path=log_path,
            popen=lambda *a, **kw: mid_proc,
            cancel_event=mid_cancel,
            poll_interval_s=0.0,
            grace_s=0.0,
        )
    assert "已中止" in c2.value.message and any(pid == 77003 for pid, _sig in killpg_calls)

    # 4. Popen 抛 OSError → LogicError(dependency_failed) 且不泄密
    with pytest.raises(LogicError) as os_err:
        run_uv_streaming(
            ["uv", "pip", "install"],
            env={"HTTPS_PROXY": "http://u:secret@127.0.0.1:7890"},
            what="安装识别依赖（cuda）",
            report=lambda _p: None,
            os_name="linux",
            log_path=log_path,
            popen=lambda *a, **kw: (_ for _ in ()).throw(OSError("spawn failed http://u:secret@127.0.0.1:7890")),
        )
    assert os_err.value.code == ErrorCode.dependency_failed and "启动失败" in os_err.value.message
    assert "secret" not in os_err.value.message and str(log_path) in os_err.value.message
