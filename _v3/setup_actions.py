"""设置页的真实动作（生产入口注入；测试只用假传输/假进程，不触网、不调 live systemctl）。

- ollama_pull：Ollama 官方 POST /api/pull 流式进度（https://docs.ollama.com/api/pull）。
- ollama_install：本平台官方发行包（ollama_asset）+ 官方 sha256sum.txt 校验，解压到用户目录，
  不用 sudo、不写系统目录（https://docs.ollama.com/linux 手动安装流程的用户级变体）。
- asr_download：在隔离 ASR 环境里用官方 huggingface_hub snapshot_download 下载 Qwen3-ASR 权重。
每个动作失败都抛 LogicError，成功前必须复查真实状态。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from _contracts import ErrorCode, LogicError, SetupProgress
from _v3.release_download import download_verified, extract_archive, percent
from _v3.setup_probe import probe_asr_weights
from _v3.setup_tasks import ProgressFn, SetupAction

Runner = Callable[..., subprocess.CompletedProcess]
OLLAMA_RELEASE_BASE = "https://github.com/ollama/ollama/releases/download"
_OLLAMA_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def ollama_asset(os_name: str, machine: str) -> str:
    """官方发行包名：Linux tar.zst（bin/ollama）、Windows zip（ollama.exe）、macOS zip（Ollama.app）。"""
    if os_name == "macos":
        return "Ollama-darwin.zip"
    arch = _OLLAMA_ARCH.get(machine.lower())
    if arch is None:
        raise LogicError(ErrorCode.dependency_failed, f"Ollama 没有 {machine} 架构的官方发行包")
    return f"ollama-windows-{arch}.zip" if os_name == "windows" else f"ollama-linux-{arch}.tar.zst"


def ollama_pull_action(*, client_factory: Callable[[], httpx.Client], model: str) -> SetupAction:
    def run(report: ProgressFn) -> str:
        layers: dict[str, tuple[int, int]] = {}
        try:
            with client_factory() as client:
                with client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", "replace")
                        raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取失败 HTTP {resp.status_code}: {body[:300]}")
                    last_status = ""
                    for line in resp.iter_lines():
                        if not line.strip():
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取进度无法解析: {line[:200]}") from exc
                        if item.get("error"):
                            raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取失败: {item['error']}")
                        last_status = str(item.get("status") or last_status)
                        digest = item.get("digest")
                        if digest and item.get("total"):
                            layers[digest] = (int(item.get("completed") or 0), int(item["total"]))
                        done = sum(c for c, _ in layers.values())
                        total = sum(t for _, t in layers.values())
                        report(SetupProgress(completed_bytes=done or None, total_bytes=total or None,
                                             percent=percent(done, total), stage=last_status))
                if last_status != "success":
                    raise LogicError(ErrorCode.dependency_failed, f"Ollama 拉取未完成，最后状态: {last_status or '无'}")
                tags = client.get("/api/tags")
                tags.raise_for_status()
                names = {m.get("name") for m in tags.json().get("models") or []} | {
                    m.get("model") for m in tags.json().get("models") or []
                }
        except httpx.HTTPError as exc:
            raise LogicError(ErrorCode.dependency_failed, f"无法连接 Ollama: {exc}") from exc
        if model not in names:
            raise LogicError(ErrorCode.dependency_failed, f"拉取结束但 /api/tags 中没有 {model}")
        return f"{model} 已下载"

    return run


def _ollama_partials(install_dir: Path) -> tuple[Path, Path]:
    return install_dir.with_name(f".{install_dir.name}.download"), install_dir.with_name(f".{install_dir.name}.staging")


def _remove_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def ollama_install_cleanup(install_dir: Path) -> None:
    """删除下载文件与解压目录（安装结束时、或重试前清理上次中断的半成品；不碰已安装好的 install_dir）。"""
    download, staging = _ollama_partials(install_dir)
    download.unlink(missing_ok=True)
    _remove_dir(staging)


def ollama_install_action(
    *,
    version: str,
    install_dir: Path,
    binary: Path,
    asset: str,
    client_factory: Callable[[], httpx.Client],
    run: Runner = subprocess.run,
) -> SetupAction:
    """下载本平台的官方发行包到用户目录（不用 sudo、不写系统目录）；解压后 binary 必须存在。"""
    binary_rel = binary.relative_to(install_dir)

    def install(report: ProgressFn) -> str:
        if binary.is_file():
            raise LogicError(ErrorCode.conflict, f"{install_dir} 已有 Ollama，未重复安装")
        base = f"{OLLAMA_RELEASE_BASE}/v{version}"
        download, staging = _ollama_partials(install_dir)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            with client_factory() as client:
                download_verified(client, url=f"{base}/{asset}", sums_url=f"{base}/sha256sum.txt", asset=asset,
                                  dest=download, report=report, stage="下载 Ollama 官方发行包")
            report(SetupProgress(stage="解压到用户目录"))
            _remove_dir(staging)
            staging.mkdir(parents=True)
            extract_archive(download, asset, staging, run=run)
            if not (staging / binary_rel).is_file():
                raise LogicError(ErrorCode.dependency_failed, f"发行包 {asset} 内没有 {binary_rel}")
            os.replace(staging, install_dir)
        finally:
            ollama_install_cleanup(install_dir)
        return f"Ollama {version} 已安装到 {install_dir}"

    return install


def asr_download_action(
    *,
    python: Path,
    worker: Path,
    repo_id: str,
    model_dir: Path,
    log_path: Path,
    env: dict[str, str],
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SetupAction:
    def download(report: ProgressFn) -> str:
        if not python.is_file():
            raise LogicError(ErrorCode.dependency_failed, f"识别运行环境未安装: {python}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total = {"bytes": 0}
        with log_path.open("w", encoding="utf-8") as log:
            proc = popen([str(python), str(worker), repo_id, str(model_dir)],
                         stdout=subprocess.PIPE, stderr=log, text=True, env=env)

            def read_stdout() -> None:
                for line in proc.stdout:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "total_bytes" in item:
                        total["bytes"] = int(item["total_bytes"])

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            while proc.poll() is None:
                done = probe_asr_weights(model_dir).bytes_on_disk if model_dir.exists() else 0
                report(SetupProgress(completed_bytes=done, total_bytes=total["bytes"] or None,
                                     percent=percent(done, total["bytes"]), stage="下载识别模型权重"))
                sleep(interval_s)
            reader.join(timeout=5)
        if proc.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            raise LogicError(ErrorCode.dependency_failed, f"识别模型下载失败（退出码 {proc.returncode}）: {tail}")
        probe = probe_asr_weights(model_dir)
        if not probe.complete:
            raise LogicError(ErrorCode.dependency_failed, "下载进程结束但权重不完整: " + "、".join(probe.missing))
        return f"{repo_id} 已下载"

    return download
