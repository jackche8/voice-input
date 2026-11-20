"""U6 识别运行环境安装：变体选择、uv 发行包 sha256 校验、命令序列（托管 Python → venv → 带 hash 锁文件）、
清单最后写且失败不写、重试前清理。假 HTTP 与假进程，不触网、不装任何东西。"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile

import httpx
import pytest

from _contracts import LogicError
from _paths import AppPaths
from _v3.runtime_install import (
    PYTHON_VERSION,
    UV_VERSION,
    asr_runtime_cleanup,
    asr_runtime_install_action,
    choose_variant,
    detect_variant,
    lock_file,
    uv_asset,
)

ASSET = "uv-x86_64-unknown-linux-gnu.tar.gz"


def _uv_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release(blob: bytes, digest: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert f"/{UV_VERSION}/" in request.url.path
        if request.url.path.endswith(f"/{ASSET}.sha256"):
            return httpx.Response(200, text=f"{digest}  {ASSET}\n")
        if request.url.path.endswith(f"/{ASSET}"):
            return httpx.Response(200, content=blob)
        return httpx.Response(404)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.fail_on = fail_on

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw["env"]))
        failed = self.fail_on is not None and self.fail_on in argv
        return subprocess.CompletedProcess(argv, 1 if failed else 0, "", "network unreachable" if failed else "")


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    return AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r", os_name="linux")


def _action(paths, run, client_factory, variant=("cpu", "未检测到 NVIDIA GPU")):
    return asr_runtime_install_action(paths=paths, os_name="linux", machine="x86_64", variant=lambda: variant,
                                      environ={"PATH": "/usr/bin"}, client_factory=client_factory, run=run,
                                      clock=lambda: "2026-11-20T00:00:00+00:00")


def test_variant_choice_and_detection():
    assert choose_variant("linux", "x86_64", has_nvidia_gpu=True) == "cuda"
    assert choose_variant("windows", "AMD64", has_nvidia_gpu=True) == "cpu"  # Windows + NVIDIA 本次用 cpu（已知限制）
    assert choose_variant("macos", "arm64", has_nvidia_gpu=False) == "mps"
    assert choose_variant("linux", "x86_64", has_nvidia_gpu=False) == "cpu"
    variant, why = detect_variant("linux", "x86_64", run=subprocess.run, which=lambda name: None)
    assert variant == "cpu" and "未检测到 NVIDIA GPU" in why and "nvidia-smi" in why
    smi = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "0, GPU-a, 16303, 15000, RTX 5070 Ti\n", "")  # noqa: E731
    assert detect_variant("linux", "x86_64", run=smi, which=lambda name: "/usr/bin/nvidia-smi") == ("cuda", "检测到 GPU：RTX 5070 Ti")
    assert uv_asset("windows", "AMD64") == "uv-x86_64-pc-windows-msvc.zip" and uv_asset("macos", "arm64").endswith("apple-darwin.tar.gz")


def test_installs_uv_python_venv_and_hashed_lock_then_writes_manifest_last(paths):
    blob = _uv_tarball()
    run = Recorder()
    progress = []
    message = _action(paths, run, _release(blob, hashlib.sha256(blob).hexdigest()))(progress.append)
    uv = str(paths.uv_dir / "uv")
    assert [argv for argv, _env in run.calls] == [
        [uv, "python", "install", PYTHON_VERSION],
        [uv, "venv", "--python", PYTHON_VERSION, str(paths.asr_venv)],
        [uv, "pip", "install", "--python", str(paths.asr_python), "--require-hashes", "-r", str(lock_file("cpu"))],
        [uv, "cache", "clean"],
    ]
    env = run.calls[0][1]
    assert env["UV_PYTHON_INSTALL_DIR"] == str(paths.python_dir) and env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    manifest = json.loads(paths.asr_manifest.read_text(encoding="utf-8"))
    assert manifest == {"variant": "cpu", "python": PYTHON_VERSION, "uv": UV_VERSION,
                        "lock_sha256": hashlib.sha256(lock_file("cpu").read_bytes()).hexdigest(),
                        "installed_at": "2026-11-20T00:00:00+00:00"}
    assert "cpu" in message and progress[0].stage == "识别运行环境变体 cpu：未检测到 NVIDIA GPU"
    assert progress[1].stage.startswith("下载 uv") and progress[-1].stage == "清理下载缓存"
    assert not list(paths.uv_dir.glob(".*"))  # 下载与解压临时文件已删
    with pytest.raises(LogicError):  # 已安装不重复安装
        _action(paths, run, _release(blob, hashlib.sha256(blob).hexdigest()))(progress.append)


def test_checksum_mismatch_or_failed_step_never_writes_manifest_and_cleanup_resets(paths):
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
    asr_runtime_cleanup(paths)
    assert not paths.asr_venv.exists() and not paths.asr_manifest.exists() and (paths.uv_dir / "uv").is_file()


@pytest.mark.parametrize("variant", ["cuda", "cpu", "mps"])
def test_each_variant_has_a_hash_locked_requirements_file(variant):
    text = lock_file(variant).read_text(encoding="utf-8")
    assert "--hash=sha256:" in text and "qwen-asr==0.0.6" in text and "--index-url https://pypi.org/simple" in text
    assert ("vllm==0.14.0" in text) is (variant == "cuda")
    assert ("torch-2.9.1%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl" in text) is (variant == "cpu")
