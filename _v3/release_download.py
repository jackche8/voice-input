"""官方发行包下载：按官方校验清单比对 sha256，按扩展名解压（Ollama 与 uv 的安装动作共用）。"""
from __future__ import annotations

import hashlib
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from _contracts import ErrorCode, LogicError, SetupProgress

Runner = Callable[..., subprocess.CompletedProcess]


def percent(done: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(min(100.0, done * 100.0 / total), 1)


def expected_sha256(sums_text: str, asset: str) -> str:
    """校验清单每行「<sha256> <文件名>」（文件名可带 * 或 ./ 前缀）。"""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == asset:
            return parts[0].lower()
    raise LogicError(ErrorCode.dependency_failed, f"官方校验清单中没有 {asset}")


def download_verified(
    client: httpx.Client,
    *,
    url: str,
    sums_url: str,
    asset: str,
    dest: Path,
    report: Callable[[SetupProgress], None],
    stage: str,
) -> None:
    """下载到 dest 并与官方清单比对；网络失败或 sha256 不一致都删掉 dest 并抛 LogicError。"""
    try:
        sums = client.get(sums_url)
        sums.raise_for_status()
        expected = expected_sha256(sums.text, asset)
        digest = hashlib.sha256()
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    report(SetupProgress(completed_bytes=done, total_bytes=total or None, percent=percent(done, total),
                                         stage=stage))
    except httpx.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise LogicError(ErrorCode.dependency_failed, f"下载 {asset} 失败: {exc}") from exc
    if digest.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise LogicError(ErrorCode.dependency_failed, f"{asset} 的 sha256 与官方清单不一致，已拒绝安装")


def extract_archive(archive: Path, asset: str, dest_dir: Path, *, run: Runner = subprocess.run) -> None:
    """按发行包名的扩展名解压到 dest_dir：.tar.zst 用系统 tar（标准库不支持 zstd），.tar.gz/.tgz 与 .zip 用标准库。"""
    if asset.endswith(".tar.zst"):
        proc = run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest_dir)],
                   capture_output=True, text=True, timeout=600, check=False)
        if proc.returncode != 0:
            raise LogicError(ErrorCode.dependency_failed, f"解压失败: {(proc.stderr or '').strip()[:300]}")
    elif asset.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as tar:
            tar.extractall(dest_dir, filter="data")
    elif asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    else:
        raise LogicError(ErrorCode.dependency_failed, f"不认识的发行包格式：{asset}")


__all__ = ["download_verified", "expected_sha256", "extract_archive", "percent"]
