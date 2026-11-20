"""U5 测试替身 worker：复用 `_v3.setup_worker.run_task`，动作由夹具目录里的文件控制（不触网、不装任何东西）。

环境变量 OVI_FAKE_WORKER_DIR 指向夹具目录：
- 动作先报告一次进度，再等 `release-<kind>` 出现：内容 `ok` → 成功；`fail:<原因>` → LogicError。
- worker 自己的清理（宿主应用退出时）在 `cleaned-<kind>` 追加一行。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from _contracts import ErrorCode, LogicError, SetupProgress
from _v3.setup_worker import parse_args, run_task


def main() -> int:
    args = parse_args()
    fixture = Path(os.environ["OVI_FAKE_WORKER_DIR"])
    kind = args.kind.value

    def action(report) -> str:
        report(SetupProgress(percent=25.0, stage="fixture 进行中"))
        release = fixture / f"release-{kind}"
        deadline = time.monotonic() + 30
        while not release.exists():
            if time.monotonic() > deadline:
                raise LogicError(ErrorCode.timeout, "测试未放行")
            time.sleep(0.02)
        verdict = release.read_text(encoding="utf-8")
        if verdict.startswith("fail:"):
            raise LogicError(ErrorCode.dependency_failed, verdict.removeprefix("fail:"))
        return "fixture 完成"

    def cleanup() -> None:
        with open(fixture / f"cleaned-{kind}", "a", encoding="utf-8") as fh:
            fh.write("cleaned\n")

    return run_task(task_file=args.task_file, kind=args.kind, action=action, cleanup=cleanup,
                    owner_pid=args.owner_pid, owner_identity=args.owner_identity, interval_s=0.05)


if __name__ == "__main__":
    os._exit(main())
