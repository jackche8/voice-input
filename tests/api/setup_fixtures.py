"""设置页夹具：显式假探测与固定任务状态。不触网、不调 systemctl、不读 live 文件。"""
from __future__ import annotations

from dataclasses import dataclass, field

from _contracts import ErrorCode, InstallAllStatus, LogicError, SetupCapability, SetupTask, SetupTaskKind
from _v3.component_routes import ComponentRegistry
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe, probe_capabilities
from _v3.setup_sequence import idle_status
from _v3.setup_service import SetupProbes, SetupService

FAKE_LAN_IPS = ["192.168.10.20", "10.1.2.3"]


@dataclass
class FakeWorld:
    session: str = "wayland"
    ollama: OllamaProbe = field(default_factory=lambda: OllamaProbe(True, True, "0.40.16", False, None, None))
    weights: AsrWeightsProbe = field(default_factory=lambda: AsrWeightsProbe(False, 0, ("config.json",)))
    tools: dict[str, str] = field(default_factory=lambda: {"xclip": "/usr/bin/xclip"})
    portals: dict[str, int] = field(default_factory=lambda: {
        "org.freedesktop.portal.Screenshot": 2,
        "org.freedesktop.portal.GlobalShortcuts": 1,
    })
    langs: list[str] = field(default_factory=lambda: ["eng", "chi_sim", "chi_tra", "osd"])
    keyboards: list[str] | Exception = field(default_factory=lambda: ["/dev/input/event5"])

    def capabilities(self, session: str) -> list[SetupCapability]:
        return probe_capabilities(
            session=session,
            which=self.tools.get,
            portal_version=lambda iface: (self.portals.get(iface), None if iface in self.portals else "fixture: 无此接口"),
            ocr_languages=lambda: list(self.langs),
            keyboard_devices=self._keyboards,
        )

    def _keyboards(self) -> list[str]:
        if isinstance(self.keyboards, Exception):
            raise self.keyboards
        return list(self.keyboards)

    def probes(self) -> SetupProbes:
        return SetupProbes(
            session=lambda: self.session,
            ollama=lambda: self.ollama,
            asr_weights=lambda: self.weights,
            capabilities=self.capabilities,
        )


class FixedTasks:
    """设置业务读任务状态用的固定替身（任务执行本身在 worker 进程里，见 test_setup_tasks_process）。"""

    def __init__(self, tasks: dict[SetupTaskKind, SetupTask | None] | None = None) -> None:
        self._fixed = dict(tasks or {})

    def latest(self, kind: SetupTaskKind) -> SetupTask | None:
        return self._fixed.get(kind)

    def start(self, kind: SetupTaskKind) -> SetupTask:
        raise LogicError(ErrorCode.dependency_failed, f"当前运行环境未提供动作 {kind.value}，拒绝假成功")

    def get(self, task_id: str) -> SetupTask:
        raise LogicError(ErrorCode.not_found, f"未知的设置作业 {task_id}")


class FixedSequence:
    """一键装齐的固定替身：设置检查只读它的状态（编排本身见 test_setup_sequence）。"""

    def __init__(self, status: InstallAllStatus | None = None) -> None:
        self._status = status or idle_status()

    def status(self) -> InstallAllStatus:
        return self._status

    def start(self) -> InstallAllStatus:
        raise LogicError(ErrorCode.dependency_failed, "当前运行环境未提供安装动作，拒绝假成功")


def task(state, message="", kind=SetupTaskKind.asr_download, progress=None) -> SetupTask:
    return SetupTask(task_id="t1", kind=kind, state=state, message=message, progress=progress,
                     started_at="2026-11-11T12:00:00+00:00")


def build_setup(world: FakeWorld, tasks: dict[SetupTaskKind, SetupTask | None] | None = None,
                components: ComponentRegistry | None = None):
    return SetupService(
        probes=world.probes(),
        tasks=FixedTasks(tasks),
        sequence=FixedSequence(),
        components=components or ComponentRegistry(),
    )
