"""组件子进程包装：保证应用主进程消失时组件随之退出（只用标准库，按文件路径以 `python -I` 运行）。

用法：`python -I child_guard.py --mode pdeathsig|watch --parent <应用 PID> -- <argv...>`，由
`_platform/_child_posix.py` 以新会话启动。之所以用单独的包装进程而不用 `preexec_fn`：应用主进程是多线程的，
在 fork 与 exec 之间跑 Python 代码可能死锁（Python 文档对 preexec_fn 的警告）；包装进程是单线程的新进程。

- pdeathsig（Linux）：prctl(PR_SET_PDEATHSIG, SIGTERM) 后再确认父进程仍是 --parent（否则父进程已先退出，不 exec），
  然后 execvp 成组件本身。PDEATHSIG 跨 execve 保留，PID 不变。
- watch（macOS 无 PDEATHSIG）：以同一会话启动组件，每 0.5s 查 getppid；父进程不在 → 整个进程组先 TERM、5s 后 KILL；
  组件自行退出 → 以其退出码退出。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
WATCH_INTERVAL_S = 0.5
WATCH_TERM_GRACE_S = 5.0
EXIT_PARENT_GONE = 1


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _run_pdeathsig(parent: int, argv: list[str]) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        print(f"child_guard: prctl(PR_SET_PDEATHSIG) 失败 errno={ctypes.get_errno()}", file=sys.stderr)
        return EXIT_PARENT_GONE
    if os.getppid() != parent:
        print(f"child_guard: 父进程 {parent} 已退出，不启动 {argv[0]}", file=sys.stderr)
        return EXIT_PARENT_GONE
    os.execvp(argv[0], argv)
    return EXIT_PARENT_GONE  # execvp 失败会抛 OSError，不会走到这里


def _kill_own_group(child: subprocess.Popen) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # 自己也在组里：先让组件收 TERM 体面退出
    os.killpg(os.getpgrp(), signal.SIGTERM)
    try:
        child.wait(timeout=WATCH_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    os.killpg(os.getpgrp(), signal.SIGKILL)  # 组内剩余进程连同包装自己一起结束


def _run_watch(parent: int, argv: list[str]) -> int:
    if os.getppid() != parent:
        print(f"child_guard: 父进程 {parent} 已退出，不启动 {argv[0]}", file=sys.stderr)
        return EXIT_PARENT_GONE
    child = subprocess.Popen(argv)
    while True:
        try:
            return _exit_code(child.wait(timeout=WATCH_INTERVAL_S))
        except subprocess.TimeoutExpired:
            pass
        if os.getppid() != parent:
            _kill_own_group(child)
            return EXIT_PARENT_GONE


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="child_guard")
    parser.add_argument("--mode", choices=("pdeathsig", "watch"), required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    ns = parser.parse_args(args)
    argv = ns.argv[1:] if ns.argv[:1] == ["--"] else ns.argv
    if not argv:
        parser.error("缺少要运行的组件命令（-- 之后）")
    if ns.mode == "pdeathsig":
        return _run_pdeathsig(ns.parent, argv)
    return _run_watch(ns.parent, argv)


if __name__ == "__main__":
    raise SystemExit(main())
