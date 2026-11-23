"""一键装齐（业务层）：按固定五步编排既有安装任务与组件事实，不实现下载/安装本身。

① ollama_install（已有程序 → skipped）→ ② wait_ollama（看护层报 ollama=running → done；stopped → failed；
120s 未运行 → failed）→ ③ ollama_pull（已有模型 → skipped）→ ④ asr_runtime_install（复用运行中 worker → 实时硬件探测
→ 有效清单且变体匹配跳过 → 磁盘预算预检 → 若磁盘有旧环境则先幂等停 ASR 释放锁 → 启 worker → 失败自动 retry 恢复）
→ ⑤ asr_download（权重完整 → skipped）→ 落盘 done 前对 UPGRADE_STOP_REASON 幂等发 retry 重拉起 ASR。
状态持久化到 install_all.json；服务端启动时若为 running 即在锁内置 _runner_active 后恢复。
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
    UPGRADE_STOP_REASON,
    ComponentName,
    ComponentState,
    ComponentStatus,
    ErrorCode,
    InstallAllState,
    InstallAllStatus,
    InstallStep,
    InstallStepKind,
    InstallStepState,
    LogicError,
    ProductComponentCommandEvent,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _events import EventBus
from _v3.component_routes import ComponentRegistry
from _v3.runtime_install import AsrHardwareProbe
from _v3.setup_tasks import SetupTaskManager, Spawn, thread_spawn

WAIT_OLLAMA_S = 120.0
STOP_ASR_S = 45.0
STOP_ASR_RESEND_S = 2.0
RESTART_ASR_S = 10.0
RESTART_ASR_RESEND_S = 1.0
POLL_S = 1.0


@dataclass(frozen=True)
class SequenceFacts:
    """每一步「已具备、跳过」及 Step 4 安全升级所需的只读事实判定。"""

    ollama_installed: Callable[[], bool]
    ollama_has_model: Callable[[], bool]
    asr_runtime_installed: Callable[[], bool]
    asr_weights_complete: Callable[[], bool]
    asr_hardware_probe: Callable[[], AsrHardwareProbe] | None = None
    asr_installed_variant: Callable[[], str | None] | None = None
    asr_runtime_on_disk: Callable[[], bool] | None = None
    asr_disk_shortfall: Callable[[str], str | None] | None = None
    invalidate_hardware_cache: Callable[[], None] | None = None


def idle_status() -> InstallAllStatus:
    return InstallAllStatus(state=InstallAllState.idle,
                            steps=[InstallStep(kind=kind, state=InstallStepState.pending) for kind in InstallStepKind])


def _is_upgrade_stopped(status: ComponentStatus | None) -> bool:
    return status is not None and status.state == ComponentState.stopped and status.reason == UPGRADE_STOP_REASON


class InstallSequence:
    def __init__(
        self,
        *,
        tasks: SetupTaskManager,
        facts: SequenceFacts,
        components: ComponentRegistry,
        state_file: Path,
        events: EventBus | None = None,
        spawn: Spawn = thread_spawn,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = POLL_S,
        wait_ollama_s: float = WAIT_OLLAMA_S,
        stop_asr_s: float = STOP_ASR_S,
        stop_asr_resend_s: float = STOP_ASR_RESEND_S,
        restart_asr_s: float = RESTART_ASR_S,
        restart_asr_resend_s: float = RESTART_ASR_RESEND_S,
    ) -> None:
        self._tasks = tasks
        self._facts = facts
        self._components = components
        self._events = events
        self._file = state_file
        self._spawn = spawn
        self._clock = clock
        self._poll_s = poll_s
        self._wait_ollama_s = wait_ollama_s
        self._stop_asr_s = stop_asr_s
        self._stop_asr_resend_s = stop_asr_resend_s
        self._restart_asr_s = restart_asr_s
        self._restart_asr_resend_s = restart_asr_resend_s
        self._lock = threading.Lock()
        self._status = self._load()
        self._runner_active = False
        should_resume = False
        with self._lock:
            if self._status.state == InstallAllState.running:
                self._runner_active = True
                should_resume = True
        if should_resume:
            self._spawn_runner()  # 服务端重启：恢复中断的顺序任务

    def status(self) -> InstallAllStatus:
        with self._lock:
            return self._status

    def start(self) -> InstallAllStatus:
        with self._lock:
            if self._runner_active:
                return self._status
            self._runner_active = True
            self._set(InstallAllStatus(state=InstallAllState.running, current=InstallStepKind.ollama_install,
                                       steps=idle_status().steps))
            started = self._status
        self._spawn_runner()
        return started

    # ---- 内部 --------------------------------------------------------------
    def _spawn_runner(self) -> None:
        try:
            self._spawn(self._run)
        except Exception:
            with self._lock:
                self._runner_active = False
            raise

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

    def _publish_asr_command(self, action: str) -> None:
        if self._events is not None:
            self._events.publish(ProductComponentCommandEvent(action=action, component=ComponentName.asr))

    def _invalidate_hardware_cache(self) -> None:
        if self._facts.invalidate_hardware_cache is not None:
            self._facts.invalidate_hardware_cache()

    def _run(self) -> None:
        ran_asr_runtime = False
        for kind in InstallStepKind:
            try:
                if kind == InstallStepKind.asr_runtime_install:
                    ok, ran_asr_runtime = self._run_asr_runtime_step()
                else:
                    ok = self._run_step(kind)
            except LogicError as exc:
                self._update_step(kind, state=InstallStepState.failed, message=exc.message)
                ok = False
            except Exception as exc:  # 未预期异常也要停在可见的失败，不能让整体卡在 running
                self._update_step(kind, state=InstallStepState.failed, message=f"{type(exc).__name__}: {exc}")
                ok = False
            if not ok:
                if kind == InstallStepKind.asr_download and _is_upgrade_stopped(self._components.get(ComponentName.asr)):
                    self._publish_asr_command("retry")
                self._finish(InstallAllState.failed, kind)
                return
        self._restart_asr_after_sequence(ran_asr_runtime=ran_asr_runtime)
        self._finish(InstallAllState.done, None)

    def _run_step(self, kind: InstallStepKind) -> bool:
        self._update_step(kind, state=InstallStepState.running, message="", progress=None, task_id=None)
        if kind == InstallStepKind.wait_ollama:
            return self._wait_ollama()
        if self._already_done(kind):
            self._update_step(kind, state=InstallStepState.skipped, message="已具备，跳过")
            return True
        return self._run_task(kind)

    def _run_asr_runtime_step(self) -> tuple[bool, bool]:
        """Step 4 安全升级编排：返回 (是否成功, 是否实际执行过替换/安装)。"""
        kind = InstallStepKind.asr_runtime_install
        self._update_step(kind, state=InstallStepState.running, message="", progress=None, task_id=None)
        self._invalidate_hardware_cache()
        stopped_asr = False
        try:
            # ① 优先复用已在跑的 asr_runtime_install worker（服务端重启恢复不重停 ASR、不误判磁盘）
            existing_task = self._tasks.latest(SetupTaskKind.asr_runtime_install)
            if existing_task is not None and existing_task.state == SetupTaskState.running:
                ok = self._follow_task(kind, existing_task)
                if not ok and _is_upgrade_stopped(self._components.get(ComponentName.asr)):
                    self._publish_asr_command("retry")
                return ok, True

            # ②a 实时硬件探测（不走 30s 缓存）：旧驱动 < 570 或探测故障直接失败，绝不动旧 ASR 与旧 venv
            target_variant: str | None = None
            if self._facts.asr_hardware_probe is not None:
                probe = self._facts.asr_hardware_probe()
                if probe.error is not None or probe.target_variant is None:
                    self._update_step(kind, state=InstallStepState.failed, message=probe.error or probe.detail)
                    return False, False
                target_variant = probe.target_variant

            # ③ 有效清单且变体匹配 → skipped；清单损坏（ValueError）不跳过
            if target_variant is not None and self._facts.asr_installed_variant is not None:
                try:
                    installed = self._facts.asr_installed_variant()
                except (OSError, ValueError):
                    installed = None
                if installed is not None and installed == target_variant:
                    self._update_step(kind, state=InstallStepState.skipped, message=f"已具备（{installed}），跳过")
                    return True, False
            elif self._facts.asr_runtime_installed():
                self._update_step(kind, state=InstallStepState.skipped, message="已具备，跳过")
                return True, False

            # ②b 磁盘预算预检（停服之前）：空间不足直接失败，不动旧 ASR 与旧 venv
            if target_variant is not None and self._facts.asr_disk_shortfall is not None:
                short = self._facts.asr_disk_shortfall(target_variant)
                if short is not None:
                    self._update_step(kind, state=InstallStepState.failed, message=short)
                    return False, False

            # ④ 磁盘已有旧清单或旧 venv：先幂等停 ASR 并等其落到 (stopped, pid=None)
            has_old_env = (
                self._facts.asr_runtime_on_disk()
                if self._facts.asr_runtime_on_disk is not None
                else self._facts.asr_runtime_installed()
            )
            if has_old_env:
                stopped_ok, stopped_asr = self._stop_asr_before_replace()
                if not stopped_ok:
                    return False, False

            # ⑤ 启安装 worker 并跟随至终态
            try:
                task = self._tasks.start(SetupTaskKind.asr_runtime_install)
                ok = self._follow_task(kind, task)
            except Exception:
                # ⑥ 失败恢复：若停过 ASR 或 ASR 处于升级暂停态，补发 retry 恢复旧服务
                if stopped_asr or _is_upgrade_stopped(self._components.get(ComponentName.asr)):
                    self._publish_asr_command("retry")
                raise
            if not ok and (stopped_asr or _is_upgrade_stopped(self._components.get(ComponentName.asr))):
                self._publish_asr_command("retry")
            return ok, True
        finally:
            self._invalidate_hardware_cache()

    def _stop_asr_before_replace(self) -> tuple[bool, bool]:
        """升级前幂等停 ASR：每 stop_asr_resend_s 发一次 stop，上限 stop_asr_s；返回 (已安全停止, 是否曾发 stop)。"""
        status = self._components.get(ComponentName.asr)
        if status is not None and status.state == ComponentState.stopped and status.pid is None:
            return True, False
        if self._events is None:
            return True, False
        deadline = self._clock() + self._stop_asr_s
        next_send = self._clock()
        sent = False
        while True:
            now = self._clock()
            if now >= next_send:
                self._publish_asr_command("stop")
                sent = True
                next_send = now + self._stop_asr_resend_s
            status = self._components.get(ComponentName.asr)
            if status is not None and status.state == ComponentState.stopped:
                if status.pid is None:
                    return True, sent
                if "无法结束组件进程" in (status.reason or ""):
                    self._update_step(
                        InstallStepKind.asr_runtime_install,
                        state=InstallStepState.failed,
                        message=f"升级前停止语音识别服务失败：{status.reason}",
                    )
                    return False, sent
            if now >= deadline:
                detail = "未收到看护层组件状态" if status is None else f"{status.state.value}（{status.reason or '无原因'}）"
                self._update_step(
                    InstallStepKind.asr_runtime_install,
                    state=InstallStepState.failed,
                    message=f"等待语音识别服务停止超时（{self._stop_asr_s:g}s）：{detail}；已中止升级，未改动旧运行环境",
                )
                return False, sent
            self._update_step(
                InstallStepKind.asr_runtime_install,
                message="正在停止语音识别服务以准备升级运行环境…",
            )
            time.sleep(self._poll_s)

    def _restart_asr_after_sequence(self, *, ran_asr_runtime: bool) -> None:
        """Step 5 完成后、done 落盘前：若处于升级暂停态则每 ~1s 发 retry 直至离态（上限 10s）；其余跑过 Step 4 发一次非阻塞 retry。"""
        if self._events is None:
            return
        if _is_upgrade_stopped(self._components.get(ComponentName.asr)):
            deadline = self._clock() + self._restart_asr_s
            next_send = self._clock()
            while True:
                now = self._clock()
                if now >= next_send:
                    self._publish_asr_command("retry")
                    next_send = now + self._restart_asr_resend_s
                if not _is_upgrade_stopped(self._components.get(ComponentName.asr)):
                    return
                if now >= deadline:
                    return
                time.sleep(self._poll_s)
        elif ran_asr_runtime:
            self._publish_asr_command("retry")

    def _already_done(self, kind: InstallStepKind) -> bool:
        return {
            InstallStepKind.ollama_install: self._facts.ollama_installed,
            InstallStepKind.ollama_pull: self._facts.ollama_has_model,
            InstallStepKind.asr_runtime_install: self._facts.asr_runtime_installed,
            InstallStepKind.asr_download: self._facts.asr_weights_complete,
        }[kind]()

    def _run_task(self, kind: InstallStepKind) -> bool:
        task = self._tasks.start(SetupTaskKind(kind.value))
        return self._follow_task(kind, task)

    def _follow_task(self, kind: InstallStepKind, task: SetupTask) -> bool:
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


__all__ = [
    "RESTART_ASR_RESEND_S",
    "RESTART_ASR_S",
    "STOP_ASR_RESEND_S",
    "STOP_ASR_S",
    "WAIT_OLLAMA_S",
    "InstallSequence",
    "SequenceFacts",
    "idle_status",
]
