"""一键装齐（业务层）：按固定五步编排既有安装任务与组件事实，不实现下载/安装本身。

① ollama_install（已有程序 → skipped）→ ② wait_ollama（看护层报 ollama=running → done；stopped → failed；
120s 未运行 → failed）→ ③ ollama_pull（已有模型 → skipped）→ ④ asr_runtime_install（已有运行环境清单 → skipped）
→ ⑤ asr_download（权重完整 → skipped）。任务步经 SetupTaskManager 起 worker，每轮读任务把进度写进本步；
任务 failed / interrupted → 本步与整体 failed、停止后续。再次开始 = 从第一步重新判定，失败那步重新执行
（管理器在重试前清理半成品）。状态持久化到 install_all.json；服务端启动时若为 running 即恢复（当前步的 worker
仍在跑时，start(kind) 返回同一任务，继续等它）。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from _atomic_file import atomic_write_bytes
from _contracts import (
    ComponentName,
    ComponentState,
    ErrorCode,
    InstallAllState,
    InstallAllStatus,
    InstallStep,
    InstallStepKind,
    InstallStepState,
    LogicError,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.component_routes import ComponentRegistry
from _v3.setup_tasks import SetupTaskManager, Spawn, thread_spawn

WAIT_OLLAMA_S = 120.0
POLL_S = 1.0


@dataclass(frozen=True)
class SequenceFacts:
    """每一步「已具备、跳过」的事实判定（只读）。"""

    ollama_installed: Callable[[], bool]
    ollama_has_model: Callable[[], bool]
    asr_runtime_installed: Callable[[], bool]
    asr_weights_complete: Callable[[], bool]


def idle_status() -> InstallAllStatus:
    return InstallAllStatus(state=InstallAllState.idle,
                            steps=[InstallStep(kind=kind, state=InstallStepState.pending) for kind in InstallStepKind])


class InstallSequence:
    def __init__(
        self,
        *,
        tasks: SetupTaskManager,
        facts: SequenceFacts,
        components: ComponentRegistry,
        state_file: Path,
        spawn: Spawn = thread_spawn,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = POLL_S,
        wait_ollama_s: float = WAIT_OLLAMA_S,
    ) -> None:
        self._tasks = tasks
        self._facts = facts
        self._components = components
        self._file = state_file
        self._spawn = spawn
        self._clock = clock
        self._poll_s = poll_s
        self._wait_ollama_s = wait_ollama_s
        self._lock = threading.Lock()
        self._status = self._load()
        self._runner_active = False
        if self._status.state == InstallAllState.running:
            self._launch()  # 服务端重启：恢复中断的顺序任务

    def status(self) -> InstallAllStatus:
        with self._lock:
            return self._status

    def start(self) -> InstallAllStatus:
        with self._lock:
            if self._runner_active:
                return self._status
            self._set(InstallAllStatus(state=InstallAllState.running, current=InstallStepKind.ollama_install,
                                       steps=idle_status().steps))
        self._launch()
        return self.status()

    # ---- 内部 --------------------------------------------------------------
    def _load(self) -> InstallAllStatus:
        try:
            return InstallAllStatus.model_validate(json.loads(self._file.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return idle_status()
        except (OSError, ValueError) as exc:
            raise LogicError(ErrorCode.internal, f"一键装齐状态文件 {self._file} 无法读取：{exc}") from exc

    def _set(self, status: InstallAllStatus) -> None:
        self._status = status
        atomic_write_bytes(self._file, status.model_dump_json().encode("utf-8"), 0o600)

    def _update_step(self, kind: InstallStepKind, **fields) -> None:
        with self._lock:
            steps = [step.model_copy(update=fields) if step.kind == kind else step for step in self._status.steps]
            self._set(self._status.model_copy(update={"current": kind, "steps": steps}))

    def _finish(self, state: InstallAllState, current: InstallStepKind | None) -> None:
        with self._lock:
            self._set(self._status.model_copy(update={"state": state, "current": current}))
            self._runner_active = False

    def _launch(self) -> None:
        with self._lock:
            self._runner_active = True
        self._spawn(self._run)

    def _run(self) -> None:
        for kind in InstallStepKind:
            try:
                ok = self._run_step(kind)
            except LogicError as exc:
                self._update_step(kind, state=InstallStepState.failed, message=exc.message)
                ok = False
            except Exception as exc:  # 未预期异常也要停在可见的失败，不能让整体卡在 running
                self._update_step(kind, state=InstallStepState.failed, message=f"{type(exc).__name__}: {exc}")
                ok = False
            if not ok:
                self._finish(InstallAllState.failed, kind)
                return
        self._finish(InstallAllState.done, None)

    def _run_step(self, kind: InstallStepKind) -> bool:
        self._update_step(kind, state=InstallStepState.running, message="", progress=None, task_id=None)
        if kind == InstallStepKind.wait_ollama:
            return self._wait_ollama()
        if self._already_done(kind):
            self._update_step(kind, state=InstallStepState.skipped, message="已具备，跳过")
            return True
        return self._run_task(kind)

    def _already_done(self, kind: InstallStepKind) -> bool:
        return {
            InstallStepKind.ollama_install: self._facts.ollama_installed,
            InstallStepKind.ollama_pull: self._facts.ollama_has_model,
            InstallStepKind.asr_runtime_install: self._facts.asr_runtime_installed,
            InstallStepKind.asr_download: self._facts.asr_weights_complete,
        }[kind]()

    def _run_task(self, kind: InstallStepKind) -> bool:
        task = self._tasks.start(SetupTaskKind(kind.value))
        while True:
            self._update_step(kind, task_id=task.task_id, progress=task.progress, message=task.message)
            if task.state == SetupTaskState.succeeded:
                self._update_step(kind, state=InstallStepState.done)
                return True
            if task.state in (SetupTaskState.failed, SetupTaskState.interrupted):
                self._update_step(kind, state=InstallStepState.failed)
                return False
            time.sleep(self._poll_s)
            task = self._tasks.get(task.task_id)

    def _wait_ollama(self) -> bool:
        deadline = self._clock() + self._wait_ollama_s
        while True:
            status = self._components.get(ComponentName.ollama)
            if status is not None and status.state == ComponentState.running:
                self._update_step(InstallStepKind.wait_ollama, state=InstallStepState.done, message="Ollama 已运行")
                return True
            if status is not None and status.state == ComponentState.stopped:
                self._update_step(InstallStepKind.wait_ollama, state=InstallStepState.failed,
                                  message=f"Ollama 已停止：{status.reason}")
                return False
            if self._clock() >= deadline:
                now = "未收到看护层状态" if status is None else f"{status.state.value}（{status.reason or '无原因'}）"
                self._update_step(InstallStepKind.wait_ollama, state=InstallStepState.failed,
                                  message=f"Ollama 未在 {self._wait_ollama_s:g} 秒内运行：{now}")
                return False
            self._update_step(InstallStepKind.wait_ollama,
                              message="等待 Ollama 运行" if status is None else f"等待 Ollama 运行（{status.state.value}）")
            time.sleep(self._poll_s)


__all__ = ["InstallSequence", "SequenceFacts", "WAIT_OLLAMA_S", "idle_status"]
