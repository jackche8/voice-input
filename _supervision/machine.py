"""单组件看护状态机（纯函数：时钟与探测由调用方注入，只在看护线程里调用）。

状态：not_installed → starting → running；失败 → backing_off（2/4/8/16s）→ starting；连续失败 5 次 → stopped。
- not_installed：每轮按文件事实重查 installed()，装好即进入拉起流程（不需要任何「已安装」通知）。
- 拉起前：端口被别的进程占用 → stopped（不计失败、不自动重试）；显存预检不过 → stopped。
- 失败（进程退出 / 启动超时 / 拉起出错）：先给出「终止整个会话」意图，再记失败、进入退避或停止。
- running 持续 60s → 连续失败清零。retry 命令：stopped / backing_off → 立即重新走拉起流程，失败计数清零。
- 进入 stopped（原因不是「应用退出」）时恰给出一次通知意图：server → 托盘，ollama/asr → 底栏 error。
时间：内部一律用单调时钟秒数；对外状态的 ISO 时间由 to_status 按「当前墙钟 − 经过的单调时长」换算。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from _contracts import ComponentName, ComponentState, ComponentStatus
from _supervision.spec import ComponentSpec

MAX_CONSECUTIVE_FAILURES = 5
BACKOFF_S = (2, 4, 8, 16)
STABLE_RESET_S = 60
APP_EXIT_REASON = "应用退出"


@dataclass(frozen=True)
class Spawn:
    """调用方据此拉起进程，成功后调用 spawned(m, pid)，失败调用 failed(...)。"""


@dataclass(frozen=True)
class TerminateSession:
    """调用方终止该组件整个会话里仍存活的进程（组件已退出也要做：清掉残留的孙进程）。"""


@dataclass(frozen=True)
class Notify:
    channel: Literal["tray", "error"]
    reason: str


Intent = Spawn | TerminateSession | Notify


def _port_free() -> str | None:
    return None


@dataclass(frozen=True)
class Probe:
    """本轮观察：组件进程退出码（仍在运行或没有进程 → None）；端口占用者描述（空闲 → None）只在要拉起时才查。"""

    exit_code: int | None = None
    port_owner: Callable[[], str | None] = _port_free


@dataclass(frozen=True)
class Machine:
    name: ComponentName
    state: ComponentState
    since: float
    reason: str | None = None
    failures: int = 0
    retry_at: float | None = None
    pid: int | None = None
    last_ready: str | None = None


def initial(name: ComponentName, now: float) -> Machine:
    return Machine(name=name, state=ComponentState.not_installed, since=now, reason="尚未检查")


def _enter(m: Machine, state: ComponentState, now: float, **changes) -> Machine:
    return replace(m, state=state, since=now, retry_at=None, last_ready=None, **changes)


def _stop(m: Machine, now: float, reason: str) -> tuple[Machine, tuple[Intent, ...]]:
    channel = "tray" if m.name == ComponentName.server else "error"
    return _enter(m, ComponentState.stopped, now, reason=reason, pid=None), (Notify(channel, reason),)


def _try_start(m: Machine, spec: ComponentSpec, probe: Probe, now: float) -> tuple[Machine, tuple[Intent, ...]]:
    owner = probe.port_owner()
    if owner is not None:
        return _stop(m, now, f"端口 {spec.port} 被 {owner}占用，未抢占")
    refused = spec.preflight()
    if refused is not None:
        return _stop(m, now, refused)
    return _enter(m, ComponentState.starting, now, reason=None, pid=None), (Spawn(),)


def failed(m: Machine, spec: ComponentSpec, now: float, reason: str) -> tuple[Machine, tuple[Intent, ...]]:
    """记一次失败：先终止会话，再退避或（第 5 次）停止。"""
    failures = m.failures + 1
    if failures >= MAX_CONSECUTIVE_FAILURES:
        stopped, notify = _stop(replace(m, failures=failures), now,
                                f"连续失败 {failures} 次已停止：{reason}（日志 {spec.log_path}）")
        return stopped, (TerminateSession(), *notify)
    backing = _enter(m, ComponentState.backing_off, now, reason=reason, failures=failures, pid=None)
    return replace(backing, retry_at=now + BACKOFF_S[failures - 1]), (TerminateSession(),)


def spawned(m: Machine, pid: int) -> Machine:
    return replace(m, pid=pid)


def step(m: Machine, spec: ComponentSpec, probe: Probe, now: float) -> tuple[Machine, tuple[Intent, ...]]:
    """推进一轮。probe.port_owner 只在要拉起时才被调用（查 /proc 有开销，不必每轮都查）。"""
    if m.state == ComponentState.not_installed:
        missing = spec.installed()
        if missing is not None:
            return (m if missing == m.reason else replace(m, reason=missing)), ()
        return _try_start(m, spec, probe, now)
    if m.state == ComponentState.backing_off:
        return _try_start(m, spec, probe, now) if m.retry_at is not None and now >= m.retry_at else (m, ())
    if m.state in (ComponentState.starting, ComponentState.running) and probe.exit_code is not None:
        phase = "启动中" if m.state == ComponentState.starting else "运行中"
        return failed(m, spec, now, f"{phase}退出（退出码 {probe.exit_code}）")
    if m.state == ComponentState.starting:
        ready = spec.ready()
        if ready.ok:
            return _enter(m, ComponentState.running, now, reason=None), ()
        if now - m.since >= spec.start_timeout_s:
            return failed(m, spec, now, f"启动超时 {spec.start_timeout_s:g}s：{ready.detail or m.last_ready or '未就绪'}")
        return replace(m, last_ready=ready.detail), ()
    if m.state == ComponentState.running and m.failures and now - m.since >= STABLE_RESET_S:
        return replace(m, failures=0), ()
    return m, ()


def retry(m: Machine, now: float) -> Machine:
    """用户点「重试」：stopped / backing_off 下一轮立即重新走拉起流程（仍先查端口与预检），失败计数清零。"""
    if m.state not in (ComponentState.stopped, ComponentState.backing_off):
        return m
    return replace(_enter(m, ComponentState.backing_off, now, reason=m.reason, failures=0), retry_at=now)


def stop(m: Machine, now: float) -> tuple[Machine, tuple[Intent, ...]]:
    """应用退出：有进程的先终止会话；状态置 stopped，原因「应用退出」，不发通知。"""
    intents: tuple[Intent, ...] = (TerminateSession(),) if m.pid is not None else ()
    return _enter(m, ComponentState.stopped, now, reason=APP_EXIT_REASON, pid=None), intents


def _iso(mono: float, *, mono_now: float, wall_now: float) -> str:
    return datetime.fromtimestamp(wall_now - (mono_now - mono), timezone.utc).isoformat()


def to_status(m: Machine, *, mono_now: float, wall_now: float) -> ComponentStatus:
    return ComponentStatus(
        name=m.name,
        state=m.state,
        reason=m.reason,
        failures=m.failures,
        retry_at=None if m.retry_at is None else _iso(m.retry_at, mono_now=mono_now, wall_now=wall_now),
        pid=m.pid,
        since=_iso(m.since, mono_now=mono_now, wall_now=wall_now),
    )


__all__ = [
    "APP_EXIT_REASON",
    "BACKOFF_S",
    "MAX_CONSECUTIVE_FAILURES",
    "STABLE_RESET_S",
    "Intent",
    "Machine",
    "Notify",
    "Probe",
    "Spawn",
    "TerminateSession",
    "failed",
    "initial",
    "retry",
    "spawned",
    "step",
    "stop",
    "to_status",
]
