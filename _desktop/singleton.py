"""跨平台守护进程单实例文件锁（POSIX fcntl / Windows msvcrt）。锁路径由调用方给（AppPaths.lock），严禁写入 `/tmp`。"""
from __future__ import annotations

import os
from pathlib import Path


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


def read_lock_owner(lock_path: Path) -> str:
    """持锁实例写在锁文件里的 PID（读不到 → unknown）。"""
    try:
        return Path(lock_path).read_text(encoding="ascii", errors="replace").strip() or "unknown"
    except OSError:
        return "unknown"


def try_acquire_singleton_lock(lock_path: Path) -> SingletonLock | None:
    """获取跨平台排他非阻塞单实例锁；已被其它进程持有 → None（调用方去唤起已运行的实例）。"""
    path = Path(lock_path)
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
        os.close(fd)
        return None

    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    return SingletonLock(fd, path)
