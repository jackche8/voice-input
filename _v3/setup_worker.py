"""安装 worker 进程：执行一个设置动作，把进度与结果写进任务文件；宿主应用退出即中止并清理半成品。

用法（由 SetupTaskManager 拉起，新会话）：
  python -m _v3.setup_worker --task-file <f> --kind <k> --owner-pid <应用 PID> --owner-identity <身份>
本进程是任务文件的唯一写者：开始时写 running（含自己的 pid 与身份），之后每轮写最新进度，最后写终态。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from _contracts import ErrorCode, LogicError, SetupProgress, SetupTask, SetupTaskKind, SetupTaskState
from _platform.child_process import process_identity
from _v3.setup_tasks import SetupAction, now_iso, write_task_file

APP_EXIT_MESSAGE = "应用已退出，安装已中止"
NO_OWNER_MESSAGE = "无法确认宿主应用进程（--owner-identity 为空）：应用退出时将无法中止安装，拒绝开始"
EXIT_FAILED = 1
EXIT_INTERRUPTED = 3


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m _v3.setup_worker")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--kind", type=SetupTaskKind, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-identity", default="")
    return parser.parse_args(argv)


def kill_own_process_group() -> None:
    """结束本 worker 派生的子进程（同一进程组），自己不退出。只在 worker 进程（新会话组长）里调用。"""
    if os.name != "posix":
        return  # Windows：worker 的子进程不在 Job 内，随其自然结束（已知限制）
    if os.getpgrp() != os.getpid():
        raise RuntimeError("kill_own_process_group 只能在以新会话启动的 worker 里调用")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)


def run_task(
    *,
    task_file: Path,
    kind: SetupTaskKind,
    action: SetupAction,
    cleanup: Callable[[], None],
    owner_pid: int,
    owner_identity: str,
    identity: Callable[[int], str | None] = process_identity,
    abort_children: Callable[[], None] = kill_own_process_group,
    interval_s: float = 1.0,
    clock: Callable[[], str] = now_iso,
) -> int:
    pid = os.getpid()
    me = identity(pid)
    task = SetupTask(task_id=task_file.stem, kind=kind, state=SetupTaskState.running, message="已开始", started_at=clock())

    def write(current: SetupTask) -> None:
        write_task_file(task_file, current, pid=pid, identity=me, owner_pid=owner_pid)

    if not owner_identity:  # 没有宿主身份就无从判断应用是否已退出，安装可能在应用退出后一直跑下去
        write(task.model_copy(update={"state": SetupTaskState.failed, "error": ErrorCode.dependency_failed,
                                      "message": NO_OWNER_MESSAGE, "finished_at": clock()}))
        return EXIT_FAILED
    write(task)
    latest: dict[str, SetupProgress | None] = {"progress": None}
    outcome: dict[str, object] = {}

    def report(progress: SetupProgress) -> None:
        latest["progress"] = progress  # 只记最新一份；主循环每轮写一次文件，不随下载块频繁写盘

    def body() -> None:
        try:
            outcome["message"] = action(report)
        except LogicError as exc:
            outcome["error"] = exc
        except Exception as exc:  # 未预期异常也必须写进任务文件，不能让任务停在 running
            outcome["error"] = LogicError(ErrorCode.internal, f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=body, name=f"setup-{kind.value}", daemon=True)
    worker.start()
    written = None
    while True:
        worker.join(interval_s)
        progress = latest["progress"]
        if progress is not None and progress is not written:
            task = task.model_copy(update={"progress": progress, "message": progress.stage or "进行中"})
            write(task)
            written = progress
        if not worker.is_alive():
            break
        if (identity(owner_pid) or "") != owner_identity:
            abort_children()
            cleanup()
            write(task.model_copy(update={"state": SetupTaskState.interrupted, "message": APP_EXIT_MESSAGE,
                                          "finished_at": clock()}))
            return EXIT_INTERRUPTED
    error = outcome.get("error")
    if isinstance(error, LogicError):
        write(task.model_copy(update={"state": SetupTaskState.failed, "error": error.code, "message": error.message,
                                      "finished_at": clock()}))
        return EXIT_FAILED
    write(task.model_copy(update={"state": SetupTaskState.succeeded, "error": None, "message": str(outcome["message"]),
                                  "finished_at": clock()}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from _v3.production import setup_worker_kind

    args = parse_args(argv)
    action, cleanup = setup_worker_kind(args.kind, os.environ)
    return run_task(task_file=args.task_file, kind=args.kind, action=action, cleanup=cleanup,
                    owner_pid=args.owner_pid, owner_identity=args.owner_identity)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # 中止时动作线程可能还卡在网络读：直接退出，不等它
