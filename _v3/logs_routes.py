"""运行日志与关于页面 API（仅本机只读与安全清空）。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from _contracts import (
    AboutInfoResponse,
    DiagnosticSummaryResponse,
    ErrorCode,
    InstallAllState,
    InstallStepKind,
    InstallStepState,
    LogClearResponse,
    LogReadResponse,
    LogSourceListResponse,
    LogicError,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.runtime import Runtime

_STEP_TO_TASK_KIND: dict[InstallStepKind, SetupTaskKind] = {
    InstallStepKind.ollama_install: SetupTaskKind.ollama_install,
    InstallStepKind.ollama_pull: SetupTaskKind.ollama_pull,
    InstallStepKind.asr_runtime_install: SetupTaskKind.asr_runtime_install,
    InstallStepKind.asr_download: SetupTaskKind.asr_download,
}


def active_setup_log_names_for(setup: object | None) -> set[str]:
    """收集当前活跃安装任务对应的规范日志文件名（双检 tasks.latest 与 sequence.status，容忍 setup=None）。"""
    if setup is None:
        return set()
    active: set[str] = set()
    tasks = getattr(setup, "tasks", None)
    if tasks is not None:
        for kind in SetupTaskKind:
            latest = tasks.latest(kind)
            if latest is not None and latest.state == SetupTaskState.running:
                active.add(f"setup-{kind.value}.log")
    sequence = getattr(setup, "sequence", None)
    if sequence is not None:
        seq_status = sequence.status()
        if seq_status.state == InstallAllState.running:
            if seq_status.current in _STEP_TO_TASK_KIND:
                active.add(f"setup-{_STEP_TO_TASK_KIND[seq_status.current].value}.log")
            for step in seq_status.steps:
                if step.state == InstallStepState.running and step.kind in _STEP_TO_TASK_KIND:
                    active.add(f"setup-{_STEP_TO_TASK_KIND[step.kind].value}.log")
    return active


def build_logs_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["logs"])

    def _logs():
        if runtime.logs is None:
            raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入运行日志服务")
        return runtime.logs

    @router.get("/api/v1/logs", response_model=LogSourceListResponse)
    def list_logs() -> LogSourceListResponse:
        return _logs().list_sources()

    # 必须在 /{name} 之前注册，防止 diagnostic-summary 被吞为路径参数
    @router.get("/api/v1/logs/diagnostic-summary", response_model=DiagnosticSummaryResponse)
    def diagnostic_summary() -> DiagnosticSummaryResponse:
        return _logs().diagnostic_summary()

    @router.get("/api/v1/logs/{name}", response_model=LogReadResponse)
    def read_log(
        name: str,
        lines: int | None = Query(default=None, ge=1, le=5000),
    ) -> LogReadResponse:
        return _logs().read(name, lines=lines)

    @router.delete("/api/v1/logs/{name}", response_model=LogClearResponse)
    def clear_log(name: str) -> LogClearResponse:
        logs = _logs()
        if name in active_setup_log_names_for(runtime.setup):
            raise LogicError(ErrorCode.conflict, f"日志文件 {name} 对应的安装任务正在运行，请等待结束后再清空")
        return logs.clear(name)

    @router.get("/api/v1/about", response_model=AboutInfoResponse)
    def get_about() -> AboutInfoResponse:
        if runtime.about is not None:
            return runtime.about()
        if runtime.logs is not None:
            return runtime.logs.about()
        raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入关于信息服务")

    return router


__all__ = ["active_setup_log_names_for", "build_logs_router"]
