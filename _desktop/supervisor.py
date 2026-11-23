"""看护运行器（应用主进程，业务层）：按顺序拉起 Ollama → ASR → 服务端，轮询、退避、停止、上报、处理重试命令。

- 独立线程 `supervisor`（非守护）运行，每 0.5s 一轮；只有它会拉起子进程（Linux PDEATHSIG 绑定创建子进程的线程），
  主线程从不等它。状态变化经 `status.set_components` 上报服务端（PlatformStatus 自有发送线程）。
- 每轮重建规格（识别运行环境可能刚装好）；状态推进全部交给 `_supervision.machine`，本模块只执行意图：
  拉起 / 终止整个会话 / 停止通知（ollama、asr → 底栏 error；server → 主线程置托盘错误态并显示「重试启动本地服务」）。
- 进程记录 run/children.json 在每次拉起与退出后重写；启动时先按记录清理上次运行的残留会话。
- 本线程绝不能因异常退出：PDEATHSIG 绑在它上面，退出会连带结束全部组件且不再拉起。单个动作失败（结束会话、
  写记录、拉起前准备）记为该组件失败并附原因；其余意外在循环外层兜底：记日志、经主线程把托盘置错，继续照看。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from dataclasses import replace
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from _contracts import ComponentName, ComponentState, ComponentStatus, ErrorCode
from _paths import AppPaths
from _platform.child_process import ChildHandle, SessionRecord, process_identity, session_record, spawn_child
from _supervision import machine as sm
from _supervision.ports import port_owner
from _supervision.records import ChildrenRecord, clean_stale_children, write_record
from _supervision.spec import ComponentSpec, build_specs

TICK_S = 0.5
LABELS: Mapping[ComponentName, str] = {
    ComponentName.ollama: "Ollama 纠错/翻译服务",
    ComponentName.asr: "语音识别服务",
    ComponentName.server: "本地服务",
}


class StatusSink(Protocol):
    def error(self, code: ErrorCode, message: str) -> None: ...
    def set_components(self, components: list[ComponentStatus]) -> None: ...


class TrayLike(Protocol):
    def set_state(self, state: str, tooltip: str | None = None) -> None: ...
    def set_server_stopped(self, stopped: bool) -> None: ...


Spawn = Callable[..., ChildHandle]


class Supervisor:
    def __init__(
        self,
        *,
        specs: Callable[[], Sequence[ComponentSpec]],
        records_path: Path,
        status: StatusSink,
        tray: TrayLike,
        ui: Callable[[Callable[[], None]], None],
        before_spawn: Mapping[ComponentName, Callable[[], None]] | None = None,
        spawn: Spawn = spawn_child,
        owner_of_port: Callable[[int], str | None] = port_owner,
        on_server_running: Callable[[], None] | None = None,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        tick_s: float = TICK_S,
    ) -> None:
        self._specs = specs
        self._records_path = records_path
        self._status = status
        self._tray = tray
        self._ui = ui
        self._before_spawn = dict(before_spawn or {})
        self._spawn = spawn
        self._owner_of_port = owner_of_port
        self._mono = mono
        self._wall = wall
        self._tick_s = tick_s
        self._on_server_running = on_server_running  # 服务端第一次进入 running 时在主线程调用一次（打开主窗口）
        now = mono()
        self._machines: dict[ComponentName, sm.Machine] = {name: sm.initial(name, now) for name in ComponentName}
        self._handles: dict[ComponentName, ChildHandle] = {}
        self._leftovers: dict[str, SessionRecord] = {}  # 没能结束干净的会话：留在进程记录里，下次启动再清
        self._specs_by_name: dict[ComponentName, ComponentSpec] = {}
        self._stopping = False
        self._last_fault: str | None = None
        self._commands: queue.Queue[tuple[Literal["retry", "stop"], ComponentName, str | None]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot: list[ComponentStatus] = []
        self._thread: threading.Thread | None = None

    # ---- 对外 ----------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="supervisor", daemon=False)
        self._thread.start()

    def retry(self, name: ComponentName | str) -> None:
        """设置页/托盘「重试」：线程安全，下一轮处理。"""
        self._commands.put(("retry", ComponentName(name), None))

    def stop_component(self, name: ComponentName | str, reason: str = sm.UPGRADE_STOP_REASON) -> None:
        """主动停服（如升级识别运行环境）：仅入队，绝不阻塞调用方/Qt 主线程；下一轮看护线程处理。"""
        self._commands.put(("stop", ComponentName(name), reason))

    def stop(self, timeout_s: float) -> None:
        """退出路径调用：看护线程按 server → asr → ollama 逆序终止各组件会话后结束。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise RuntimeError(f"看护线程 {timeout_s}s 内未结束（组件会话仍在终止）")

    def snapshot(self) -> list[ComponentStatus]:
        with self._lock:
            return list(self._snapshot)

    # ---- 看护线程 ------------------------------------------------------------
    def _run(self) -> None:
        try:
            clean_stale_children(self._records_path)
        except Exception as exc:  # 记录损坏或清不掉：残留若占着端口，组件会以「端口被占用」可见地停下
            self._fault(f"无法清理上次运行残留的组件进程：{type(exc).__name__}: {exc}", exc)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 兜底：见模块说明，本线程不能退出
                self._fault(f"看护出错：{type(exc).__name__}: {exc}", exc)
            self._stop.wait(self._tick_s)
        try:
            self.stop_all()
        except Exception as exc:  # 应用正在退出：记下原因，剩余组件由 PDEATHSIG / 下次启动的记录清理收尾
            self._fault(f"退出时结束组件出错：{type(exc).__name__}: {exc}", exc)

    def tick(self) -> None:
        now = self._mono()
        specs = self._specs()
        self._specs_by_name = {spec.name: spec for spec in specs}
        while not self._commands.empty():
            action, name, reason = self._commands.get_nowait()
            if action == "retry":
                self._apply(name, sm.retry(self._machines[name], now), (), spec=self._specs_by_name.get(name))
            elif action == "stop":
                current = self._machines[name]
                if current.state == ComponentState.stopped and name not in self._handles and current.pid is not None:
                    continue  # 先前终止失败保留了 pid 与原因：重复 stop 幂等保留失败态，绝不覆写
                machine, intents = sm.stop(current, now, reason=reason or sm.UPGRADE_STOP_REASON)
                self._apply(name, machine, intents, spec=self._specs_by_name.get(name))
        for spec in specs:
            handle = self._handles.get(spec.name)
            probe = sm.Probe(exit_code=None if handle is None else handle.poll(),
                             port_owner=lambda port=spec.port: self._owner_of_port(port))
            machine, intents = sm.step(self._machines[spec.name], spec, probe, now)
            self._apply(spec.name, machine, intents, spec=spec)
        self._publish()

    def stop_all(self) -> None:
        self._stopping = True
        now = self._mono()
        for name in reversed(list(ComponentName)):
            machine, intents = sm.stop(self._machines[name], now)
            self._apply(name, machine, intents, spec=self._specs_by_name.get(name))
        self._publish()

    def _apply(self, name: ComponentName, machine: sm.Machine, intents, *, spec: ComponentSpec | None = None) -> None:
        previous = self._machines[name]
        self._machines[name] = machine
        for intent in intents:
            if isinstance(intent, sm.TerminateSession):
                self._terminate(name, spec)
            elif isinstance(intent, sm.Spawn):
                self._launch(name, spec)
            elif isinstance(intent, sm.Notify):
                self._notify(name, intent)
        if name == ComponentName.server and previous.state == ComponentState.stopped and machine.state != ComponentState.stopped:
            self._ui(lambda: self._tray.set_server_stopped(False))
        if name == ComponentName.server and machine.state == ComponentState.running and self._on_server_running is not None:
            callback, self._on_server_running = self._on_server_running, None
            self._ui(callback)

    def _launch(self, name: ComponentName, spec: ComponentSpec) -> None:
        try:
            prepare = self._before_spawn.get(name)
            if prepare is not None:
                prepare()
            handle = self._spawn(spec.argv, cwd=spec.cwd, env=spec.env, log_path=spec.log_path)
        except Exception as exc:  # 拉起本身失败（程序缺失、证书生成失败…）按一次失败处理，原因可见
            machine, intents = sm.failed(self._machines[name], spec, self._mono(), f"无法启动：{type(exc).__name__}: {exc}")
            self._apply(name, machine, intents, spec=spec)
            return
        self._handles[name] = handle
        self._machines[name] = sm.spawned(self._machines[name], handle.pid)
        problem = self._write_records()
        if problem is not None:  # 没有记录就保证不了应用崩溃后能清掉它：这次拉起算失败，结束刚起的会话
            machine, intents = sm.failed(self._machines[name], spec, self._mono(), problem)
            self._apply(name, machine, intents, spec=spec)

    def _terminate(self, name: ComponentName, spec: ComponentSpec | None) -> None:
        handle = self._handles.pop(name, None)
        if handle is None:
            return
        try:
            handle.terminate_session(spec.stop_timeout_s if spec is not None else 10.0)
        except Exception as exc:  # 会话没清干净：记录保留给下次启动清理；原因并入引发本次清理的那次失败
            self._leftovers[f"{name.value}-{handle.pid}"] = session_record(handle)
            reason = f"无法结束组件进程：{type(exc).__name__}: {exc}"
            if self._stopping:
                self._fault(f"{LABELS[name]}{reason}", exc)
            else:
                # 清理失败时保留未退出进程的 pid 与原因：状态机已为这次事件计过一次失败（或已置 stopped），这里不再加计
                current = self._machines[name]
                self._machines[name] = replace(
                    current,
                    pid=handle.pid,
                    reason=f"{current.reason}；{reason}" if current.reason else reason,
                )
        problem = self._write_records()
        if problem is not None:
            self._fault(problem)

    def _notify(self, name: ComponentName, notice: sm.Notify) -> None:
        reason = self._machines[name].reason or notice.reason  # 含清理失败时并入的原因
        if notice.channel == "error":
            self._status.error(ErrorCode.dependency_failed, f"{LABELS[name]}已停止：{reason}")
            return
        tooltip = f"{LABELS[name]}已停止：{reason}"  # 服务端停止时事件通道不可用，只能由应用直接置托盘

        def show() -> None:
            self._tray.set_state("error", tooltip=tooltip)
            self._tray.set_server_stopped(True)

        self._ui(show)

    def _write_records(self) -> str | None:
        """返回失败原因（None = 已写入）。"""
        children = {name.value: session_record(handle) for name, handle in self._handles.items()}
        children.update(self._leftovers)
        pid = os.getpid()
        try:
            write_record(self._records_path, ChildrenRecord(app_pid=pid, app_identity=process_identity(pid), children=children))
        except OSError as exc:
            return f"无法写进程记录 {self._records_path}：{exc}"
        return None

    def _fault(self, message: str, exc: BaseException | None = None) -> None:
        """看护层自身出错：记日志、经主线程把托盘置为错误态（服务端可能已不在，底栏事件不一定送得到）。同一原因只报一次。"""
        if message == self._last_fault:
            return
        self._last_fault = message
        print(f"  看护: {message}", flush=True)
        if exc is not None:
            traceback.print_exception(exc)
        self._ui(lambda: self._tray.set_state("error", tooltip=message))

    def _publish(self) -> None:
        mono, wall = self._mono(), self._wall()
        snapshot = [sm.to_status(self._machines[name], mono_now=mono, wall_now=wall) for name in ComponentName]
        with self._lock:
            changed = [(s.name, s.state, s.reason, s.failures, s.pid) for s in snapshot] != [
                (s.name, s.state, s.reason, s.failures, s.pid) for s in self._snapshot]
            self._snapshot = snapshot
        if changed:
            self._status.set_components(snapshot)


def create_supervisor(
    paths: AppPaths,
    environ: Mapping[str, str],
    *,
    status: StatusSink,
    tray: TrayLike,
    ui: Callable[[Callable[[], None]], None],
    on_server_running: Callable[[], None] | None = None,
) -> Supervisor:
    """应用入口用：组件规格按 environ 每轮重建；服务端拉起前就地同步本地 CA 与服务端证书（取代单元的 ExecStartPre）。"""
    from _platform.certs import ensure_tls_certificates

    return Supervisor(
        specs=lambda: build_specs(paths, environ),
        records_path=paths.children_record,
        status=status,
        tray=tray,
        ui=ui,
        before_spawn={ComponentName.server: lambda: ensure_tls_certificates(paths.cert_dir)},
        on_server_running=on_server_running,
    )


__all__ = ["LABELS", "Supervisor", "create_supervisor"]
