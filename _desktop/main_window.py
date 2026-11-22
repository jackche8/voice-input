"""主窗口启动器（应用主进程，业务层）：托盘「打开主界面」「设置」、第二次启动唤起都走这里。

窗口进程（python -m _window）在 → 经它的 stdin 写一行 show；不在 → 经 child_process 拉起（随应用退出）。
关闭：写 quit、等 3s、仍在则结束其会话。窗口进程非 0 退出（例如缺 WebKit2GTK）→ 底栏 error「主界面无法打开：<日志末行>」。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from _paths import APP_DISPLAY_NAME, INTERNAL_WINDOW_FLAG, AppPaths, internal_subcommand
from _platform.child_process import ChildHandle, spawn_child
from _window.commands import Quit, Show, encode

CLOSE_WAIT_S = 3.0
WATCH_S = 0.2


def window_command(
    paths: AppPaths,
    origin: str,
    route: str,
    *,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    prefix = internal_subcommand(INTERNAL_WINDOW_FLAG, ("-m", "_window"), frozen=frozen, executable=executable)
    return [*prefix, "--url", f"{origin}{route}", "--cert", str(paths.cert), "--title", APP_DISPLAY_NAME]


def _last_line(log_path: Path) -> str:
    try:
        lines = [line for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except FileNotFoundError:
        return f"无输出（{log_path} 不存在）"
    return lines[-1] if lines else "无输出"


class MainWindow:
    def __init__(
        self,
        *,
        paths: AppPaths,
        origin: str,
        environ: Mapping[str, str],
        on_failed: Callable[[str], None],
        spawn: Callable[..., ChildHandle] = spawn_child,
        close_wait_s: float = CLOSE_WAIT_S,
    ) -> None:
        self._paths = paths
        self._origin = origin
        self._environ = dict(environ)
        self._on_failed = on_failed
        self._spawn = spawn
        self._close_wait_s = close_wait_s
        self._log_path = paths.logs / "window.log"
        self._lock = threading.Lock()
        self._handle: ChildHandle | None = None
        self._closing = False

    def show(self, route: str = "/") -> None:
        """在主线程调用（托盘回调 / 单例唤起）。"""
        with self._lock:
            if self._handle is not None and self._handle.poll() is None and self._send(Show(route)):
                return
            self._handle = self._spawn(window_command(self._paths, self._origin, route), cwd=self._paths.program_root,
                                       env=self._environ, log_path=self._log_path, stdin_pipe=True)
            handle = self._handle
        threading.Thread(target=self._watch, args=(handle,), name="main-window-watch", daemon=True).start()

    def close(self) -> None:
        """应用退出时调用：请窗口自己退出，3s 内未退出就结束它的会话。"""
        with self._lock:
            handle, self._closing = self._handle, True
        if handle is None or handle.poll() is not None:
            return
        self._send(Quit(), handle)
        deadline = time.monotonic() + self._close_wait_s
        while handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if handle.poll() is None:
            handle.terminate_session(2.0)

    def _send(self, command: Show | Quit, handle: ChildHandle | None = None) -> bool:
        target = handle or self._handle
        try:
            target.stdin.write(encode(command))
            target.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):  # 窗口进程刚退出：调用方据此重新拉起
            return False

    def _watch(self, handle: ChildHandle) -> None:
        while (code := handle.poll()) is None:
            time.sleep(WATCH_S)
        if code != 0 and not self._closing:
            self._on_failed(f"主界面无法打开：{_last_line(self._log_path)}")


__all__ = ["MainWindow", "window_command"]
