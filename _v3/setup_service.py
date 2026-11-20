"""设置页业务：把只读探测、安装任务状态与看护层组件快照组合成 SetupCheckResponse。

是否在运行只看看护层上报的组件状态（唯一来源，服务端只缓存）；权重、模型、版本仍按探测事实；
下载/安装进度来自安装任务；一键装齐的逐步进度在 install_all。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from _contracts import (
    AsrSetupStatus,
    ComponentName,
    ComponentState,
    ComponentStatus,
    OllamaSetupStatus,
    SetupCapability,
    SetupCheckResponse,
    SetupComponentState,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.component_routes import ComponentRegistry
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe
from _v3.setup_sequence import InstallSequence
from _v3.setup_tasks import SetupTaskManager


def size_label(num_bytes: int | None) -> str | None:
    if not num_bytes:
        return None
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return None


@dataclass(frozen=True)
class SetupProbes:
    """全部只读探测，由组合入口显式注入。"""

    session: Callable[[], str]
    ollama: Callable[[], OllamaProbe]
    asr_weights: Callable[[], AsrWeightsProbe]
    capabilities: Callable[[str], list[SetupCapability]]


def _running(task: SetupTask | None) -> bool:
    return task is not None and task.state == SetupTaskState.running


def _failed(task: SetupTask | None) -> bool:
    return task is not None and task.state == SetupTaskState.failed


ASR_LABEL = "语音识别服务"
OLLAMA_LABEL = "Ollama 纠错/翻译服务"


def component_message(label: str, status: ComponentStatus | None) -> str:
    """组件当前状态的可见说明（运行与否只来自看护层上报）。"""
    if status is None:
        return f"{label}：尚未收到应用的组件状态（应用未运行或刚启动）"
    reason = f"：{status.reason}" if status.reason else ""
    return {
        ComponentState.running: f"{label}运行中",
        ComponentState.starting: f"{label}启动中",
        ComponentState.not_installed: f"{label}未安装{reason}",
        ComponentState.backing_off: f"{label}重启中（连续失败 {status.failures} 次{reason}）",
        ComponentState.stopped: f"{label}已停止{reason}",
    }[status.state]


def _is_running(status: ComponentStatus | None) -> bool:
    return status is not None and status.state == ComponentState.running


class SetupService:
    def __init__(
        self,
        *,
        probes: SetupProbes,
        tasks: SetupTaskManager,
        sequence: InstallSequence,
        components: ComponentRegistry,
    ) -> None:
        self._probes = probes
        self.tasks = tasks
        self.sequence = sequence
        self._components = components

    def check(self) -> SetupCheckResponse:
        session = self._probes.session()
        return SetupCheckResponse(
            session_type=session,
            asr=self._asr_status(),
            ollama=self._ollama_status(),
            capabilities=self._probes.capabilities(session),
            components=self._components.snapshot(),
            install_all=self.sequence.status(),
        )

    def _asr_status(self) -> AsrSetupStatus:
        weights = self._probes.asr_weights()
        download = self.tasks.latest(SetupTaskKind.asr_download)
        component = self._components.get(ComponentName.asr)
        common = dict(
            weights_present=weights.complete,
            sidecar_running=_is_running(component),
            size_label=size_label(weights.bytes_on_disk),
        )
        if _running(download):
            return AsrSetupStatus(status=SetupComponentState.downloading, progress=download.progress,
                                  message=download.message, **common)
        if not weights.complete:
            if _failed(download):
                return AsrSetupStatus(status=SetupComponentState.failed, message=download.message, **common)
            return AsrSetupStatus(status=SetupComponentState.missing,
                                  message="识别模型未下载完整：" + "、".join(weights.missing), **common)
        # 权重齐全：status 只表示权重（ready）；运行与否看 sidecar_running，说明来自看护层组件状态
        return AsrSetupStatus(status=SetupComponentState.ready, message=component_message(ASR_LABEL, component), **common)

    def _ollama_status(self) -> OllamaSetupStatus:
        probe = self._probes.ollama()
        component = self._components.get(ComponentName.ollama)
        install = self.tasks.latest(SetupTaskKind.ollama_install)
        pull = self.tasks.latest(SetupTaskKind.ollama_pull)
        base = dict(installed=probe.installed, version=probe.version, running=_is_running(component))
        if not _is_running(component):
            if _running(install):
                return OllamaSetupStatus(model_status=SetupComponentState.missing, progress=install.progress,
                                         message=install.message, **base)
            if not probe.installed:
                return OllamaSetupStatus(model_status=SetupComponentState.missing,
                                         message=install.message if _failed(install) else "未安装用户级 Ollama", **base)
            return OllamaSetupStatus(model_status=SetupComponentState.missing,
                                     message=component_message(OLLAMA_LABEL, component), **base)
        if probe.has_model:
            return OllamaSetupStatus(model_status=SetupComponentState.ready, size_label=size_label(probe.model_size),
                                     message="纠错/翻译模型已就绪", **base)
        if _running(pull):
            return OllamaSetupStatus(model_status=SetupComponentState.downloading, progress=pull.progress,
                                     message=pull.message, **base)
        if _failed(pull):
            return OllamaSetupStatus(model_status=SetupComponentState.failed, message=pull.message, **base)
        return OllamaSetupStatus(model_status=SetupComponentState.missing,
                                 message=probe.error or "纠错/翻译模型未下载", **base)


__all__ = ["ASR_LABEL", "OLLAMA_LABEL", "SetupProbes", "SetupService", "component_message", "size_label"]
