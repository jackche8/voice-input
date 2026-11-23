"""设置页业务：把只读探测、安装任务状态与看护层组件快照组合成 SetupCheckResponse。

是否在运行只看看护层上报的组件状态（唯一来源，服务端只缓存）；权重、模型、版本仍按探测事实；
运行环境变体与硬件探测通过 30s 缓存读取并填充 `AsrSetupStatus.device_label`（异常与损坏清单全吸收，永不 500）；
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
    InstallAllState,
    InstallStepKind,
    OllamaSetupStatus,
    SetupCapability,
    SetupCheckResponse,
    SetupComponentState,
    SetupTask,
    SetupTaskKind,
    SetupTaskState,
)
from _v3.component_routes import ComponentRegistry
from _v3.runtime_install import AsrHardwareProbe
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe
from _v3.setup_sequence import InstallSequence
from _v3.setup_tasks import SetupTaskManager

_VARIANT_LABELS = {
    "cuda": "CUDA GPU",
    "mps": "Apple MPS",
    "cpu": "CPU",
}
UPGRADE_BUSY_MESSAGE = "正在安装或升级语音识别运行环境，请等待完成后再重试"


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
    asr_variant: Callable[[], str | None] | None = None
    asr_hardware: Callable[[], AsrHardwareProbe] | None = None


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

    def asr_runtime_upgrade_busy_reason(self) -> str | None:
        """Step 4（asr_runtime_install）或识别运行环境安装任务正在执行时返回 409 拒因。"""
        seq = self.sequence.status()
        if seq.state == InstallAllState.running and seq.current == InstallStepKind.asr_runtime_install:
            return UPGRADE_BUSY_MESSAGE
        if _running(self.tasks.latest(SetupTaskKind.asr_runtime_install)):
            return UPGRADE_BUSY_MESSAGE
        return None

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

    def _asr_device_label(self) -> str | None:
        """只读解析当前运行环境与硬件可升级/旧驱动提示，吸收损坏清单与硬件探测异常，永不抛出 500。"""
        installed_variant: str | None = None
        if self._probes.asr_variant is not None:
            try:
                installed_variant = self._probes.asr_variant()
            except (OSError, ValueError):
                return "运行环境清单损坏，点击「一键装齐」重建"

        hardware: AsrHardwareProbe | None = None
        hardware_error: str | None = None
        if self._probes.asr_hardware is not None:
            try:
                hardware = self._probes.asr_hardware()
            except Exception as exc:
                hardware_error = f"硬件探测异常：{exc}"

        installed_label = _VARIANT_LABELS.get(installed_variant, installed_variant) if installed_variant is not None else None
        if hardware_error is not None:
            return f"{installed_label}（{hardware_error}）" if installed_label else hardware_error
        if hardware is not None and hardware.error is not None:
            return f"{installed_label}（{hardware.error}）" if installed_label else hardware.error
        if installed_variant is not None:
            if hardware is not None and hardware.target_variant == "cuda" and installed_variant == "cpu":
                gpu_note = f"（{hardware.gpu_name}）" if hardware.gpu_name else ""
                return f"当前 CPU · 检测到 GPU{gpu_note}，点击「一键装齐」可升级 CUDA GPU 加速"
            if hardware is not None and hardware.target_variant is not None and hardware.target_variant != installed_variant:
                target_label = _VARIANT_LABELS.get(hardware.target_variant, hardware.target_variant)
                return f"当前 {installed_label} · 点击「一键装齐」可切换至 {target_label}"
            return installed_label
        if hardware is not None and hardware.target_variant is not None:
            return _VARIANT_LABELS.get(hardware.target_variant, hardware.target_variant)
        return None

    def _asr_status(self) -> AsrSetupStatus:
        weights = self._probes.asr_weights()
        download = self.tasks.latest(SetupTaskKind.asr_download)
        component = self._components.get(ComponentName.asr)
        common = dict(
            weights_present=weights.complete,
            sidecar_running=_is_running(component),
            size_label=size_label(weights.bytes_on_disk),
            device_label=self._asr_device_label(),
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


__all__ = [
    "ASR_LABEL",
    "OLLAMA_LABEL",
    "UPGRADE_BUSY_MESSAGE",
    "SetupProbes",
    "SetupService",
    "component_message",
    "size_label",
]
