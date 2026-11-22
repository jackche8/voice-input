"""PyInstaller 单可执行冻结入口：在导入任何 Qt / PySide6 模块前先按 argv[1] 派发内部子进程。

支持 5 路派发：
- `--internal-child-guard`  → `_platform.child_guard.main(rest)`
- `--internal-server`       → `uvicorn.main.main(args=rest, prog_name="uvicorn")`（显式导入 `app`）
- `--internal-setup-worker` → `_v3.setup_worker.main(rest)`（`__main__` 退出走 `os._exit(code)`）
- `--internal-window`       → `_window.__main__.main(rest)`
- 其它（无参 / `--background` / macOS `-psn_*` 等）→ `daemon.main()`
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence

from _paths import (
    INTERNAL_CHILD_GUARD_FLAG,
    INTERNAL_SERVER_FLAG,
    INTERNAL_SETUP_WORKER_FLAG,
    INTERNAL_WINDOW_FLAG,
)


def _run_child_guard(rest: list[str]) -> int:
    from _platform import child_guard

    return int(child_guard.main(rest))


def _run_server(rest: list[str]) -> int:
    import app as _app  # noqa: F401  # 确保 PyInstaller 静态分析追踪整个服务端依赖树
    import uvicorn

    try:
        uvicorn.main.main(args=rest, prog_name="uvicorn")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _run_setup_worker(rest: list[str]) -> int:
    from _v3 import setup_worker

    return int(setup_worker.main(rest))


def _run_window(rest: list[str]) -> int:
    from _window.__main__ import main as window_main

    return int(window_main(rest))


def _run_daemon() -> int:
    import daemon

    daemon.main()
    return 0


def flush_stdio() -> None:
    """Windows `console=False` 冻结态下 stdout/stderr 可能为 None，刷新前显式判空。"""
    if sys.stdout is not None:
        sys.stdout.flush()
    if sys.stderr is not None:
        sys.stderr.flush()


def dispatch(
    argv: Sequence[str],
    *,
    run_child_guard: Callable[[list[str]], int] = _run_child_guard,
    run_server: Callable[[list[str]], int] = _run_server,
    run_setup_worker: Callable[[list[str]], int] = _run_setup_worker,
    run_window: Callable[[list[str]], int] = _run_window,
    run_daemon: Callable[[], int] = _run_daemon,
) -> int:
    """按 argv[1] 派发内部子进程或应用主进程，返回整型退出码（不直接调 os._exit）。"""
    flag = argv[1] if len(argv) > 1 else ""
    rest = list(argv[2:])
    if flag == INTERNAL_CHILD_GUARD_FLAG:
        return int(run_child_guard(rest))
    if flag == INTERNAL_SERVER_FLAG:
        return int(run_server(rest))
    if flag == INTERNAL_SETUP_WORKER_FLAG:
        return int(run_setup_worker(rest))
    if flag == INTERNAL_WINDOW_FLAG:
        return int(run_window(rest))
    return int(run_daemon())


if __name__ == "__main__":
    exit_code = dispatch(sys.argv)
    flush_stdio()
    if len(sys.argv) > 1 and sys.argv[1] == INTERNAL_SETUP_WORKER_FLAG:
        os._exit(exit_code)  # 与 _v3/setup_worker.py 一致：中止时下载线程可能卡在网络读，直接退出
    raise SystemExit(exit_code)
