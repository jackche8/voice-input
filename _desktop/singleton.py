"""跨平台守护进程单实例文件锁（POSIX fcntl / Windows msvcrt），严禁写入 `/tmp`。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def default_lock_path(project_root: Path = PROJECT_ROOT) -> Path:
    """解析平台安全的锁文件路径：Linux 优先 `XDG_RUNTIME_DIR`，其余回退项目 `.cache/`。"""
    if os.name != "nt":
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir:
            base = Path(runtime_dir)
            if base.is_dir():
                return base / "ollama-voice-input.lock"
    cache_dir = Path(project_root) / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "ollama-voice-input.lock"


class SingletonLock:
    """持有打开文件描述符的单例锁句柄；调用 `release()` 或进程退出时释放。"""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_singleton_lock(lock_path: Path | None = None) -> SingletonLock:
    """获取跨平台排他非阻塞单实例锁；已被占用时打印已有 PID 并以退出码 1 退出。"""
    path = default_lock_path() if lock_path is None else Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            existing_pid = os.read(fd, 32).decode("utf-8", errors="replace").strip() or "unknown"
        except Exception:
            existing_pid = "unknown"
        os.close(fd)
        print(f"错误: 守护进程已在运行 (PID {existing_pid})，不能重复启动。")
        sys.exit(1)

    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    return SingletonLock(fd, path)
