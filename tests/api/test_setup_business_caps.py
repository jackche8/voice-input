"""业务层：快捷键冲突能力项、键盘设备能力项、ASR/Ollama 状态文案（运行与否只读看护层组件快照）。全部假探测。"""
from __future__ import annotations

import pytest

from _contracts import ComponentReport, ComponentStatus, ErrorCode, LogicError, SetupProgress, SetupTaskKind
from _linux.conflicts import ConflictScan
from _v3.component_routes import ComponentRegistry
from _v3.runtime_install import AsrHardwareProbe
from _v3.setup_probe import AsrWeightsProbe, OllamaProbe, hotkey_conflicts_capability
from _v3.setup_service import SetupService
from tests.api.setup_fixtures import FakeWorld, FixedSequence, FixedTasks, task

SINCE = "2026-11-20T00:00:00+00:00"


# ---------------------------------------------------------------- hotkey_conflicts
def test_hotkey_conflicts_none_read_is_available_with_limits_stated():
    cap = hotkey_conflicts_capability(lambda: ConflictScan((), ()))
    assert cap.id == "hotkey_conflicts" and cap.available and not cap.required
    assert "evdev" in cap.detail


def test_hotkey_conflicts_reported_and_unread_never_means_none():
    c = "screenshot alt+x 与 应用 com.pylogmon.Manggo 的全局快捷键 screenshot-recognition 的 <Alt>x 冲突"
    cap = hotkey_conflicts_capability(lambda: ConflictScan((c,), ()))
    assert not cap.available and "com.pylogmon.Manggo" in cap.detail
    cap = hotkey_conflicts_capability(lambda: ConflictScan((), ("org.gnome.mutter.keybindings：退出码 1",)))
    assert not cap.available and "未能读取" in cap.detail and "未发现冲突" not in cap.detail
    cap = hotkey_conflicts_capability(lambda: ConflictScan((c,), ("应用 x：超时",)))
    assert not cap.available and "Manggo" in cap.detail and "未能读取" in cap.detail


def test_hotkey_conflicts_scan_exception_is_visible():
    def boom():
        raise LogicError(ErrorCode.config_io, "配置文件不是合法 JSON")

    cap = hotkey_conflicts_capability(boom)
    assert not cap.available and "无法检查" in cap.detail and "合法 JSON" in cap.detail


# ---------------------------------------------------------------- evdev_keyboards
def test_wayland_requires_readable_keyboards_and_x11_does_not_list_it():
    def cap(world):
        return {c.id: c for c in world.capabilities(world.session)}.get("evdev_keyboards")

    none = cap(FakeWorld(keyboards=[]))
    assert none.required and not none.available and "input 组" in none.detail
    broken = cap(FakeWorld(keyboards=ModuleNotFoundError("No module named 'evdev'")))
    assert broken.required and not broken.available and broken.detail == "ModuleNotFoundError: No module named 'evdev'"
    ok = cap(FakeWorld(keyboards=["/dev/input/event5", "/dev/input/event16"]))
    assert ok.required and ok.available and "/dev/input/event16" in ok.detail
    assert cap(FakeWorld(session="x11", keyboards=[])) is None


# ---------------------------------------------------------------- 组件状态驱动的 asr / ollama 文案
def _registry(**states: tuple[str, str | None]) -> ComponentRegistry:
    registry = ComponentRegistry()
    if states:
        items = [ComponentStatus(name=n, state=states.get(n, ("running", None))[0], reason=states.get(n, ("running", None))[1],
                                 failures=2 if states.get(n, ("running", None))[0] == "backing_off" else 0, since=SINCE)
                 for n in ("ollama", "asr", "server")]
        registry.replace(ComponentReport(components=items))
    return registry


def _check(world: FakeWorld, registry: ComponentRegistry, tasks=None):
    svc = SetupService(probes=world.probes(), tasks=FixedTasks(tasks), sequence=FixedSequence(), components=registry)
    return svc.check()


READY_WEIGHTS = AsrWeightsProbe(True, 4_400_000_000, ())


@pytest.mark.parametrize(
    ("state", "running", "message"),
    [
        (None, False, "语音识别服务：尚未收到应用的组件状态"),
        (("running", None), True, "语音识别服务运行中"),
        (("starting", None), False, "语音识别服务启动中"),
        (("backing_off", "启动中退出（退出码 1）"), False, "语音识别服务重启中（连续失败 2 次：启动中退出（退出码 1））"),
        (("stopped", "显存不足：需要 7337 MiB"), False, "语音识别服务已停止：显存不足：需要 7337 MiB"),
        (("not_installed", "未安装识别运行环境"), False, "语音识别服务未安装：未安装识别运行环境"),
    ],
)
def test_asr_running_and_message_come_from_component_snapshot(state, running, message):
    registry = _registry() if state is None else _registry(asr=state)
    check = _check(FakeWorld(weights=READY_WEIGHTS), registry)
    assert check.asr.status.value == "ready" and check.asr.sidecar_running is running
    assert check.asr.message.startswith(message)
    assert [c.name.value for c in check.components] == ([] if state is None else ["ollama", "asr", "server"])


def test_asr_download_task_progress_and_failure_shown_until_weights_complete():
    running = task("running", "下载识别模型权重", kind=SetupTaskKind.asr_download,
                   progress=SetupProgress(completed_bytes=1, total_bytes=4, percent=25.0, stage="下载识别模型权重"))
    check = _check(FakeWorld(), _registry(), {SetupTaskKind.asr_download: running})
    assert check.asr.status.value == "downloading" and check.asr.progress.percent == 25.0
    failed = task("failed", "网络被拒绝", kind=SetupTaskKind.asr_download)
    assert _check(FakeWorld(), _registry(), {SetupTaskKind.asr_download: failed}).asr.status.value == "failed"
    assert _check(FakeWorld(weights=READY_WEIGHTS), _registry(), {SetupTaskKind.asr_download: failed}).asr.status.value == "ready"


def test_ollama_running_from_component_and_model_from_probe():
    world = FakeWorld(ollama=OllamaProbe(True, True, "0.40.16", True, 3_400_000_000, None))
    stopped = _check(world, _registry(ollama=("stopped", "端口 17947 被 PID 7（ollama）占用，未抢占"))).ollama
    assert stopped.running is False and stopped.message == "Ollama 纠错/翻译服务已停止：端口 17947 被 PID 7（ollama）占用，未抢占"
    assert stopped.model_status.value == "missing"
    up = _check(world, _registry(ollama=("running", None))).ollama
    assert up.running and up.model_status.value == "ready" and up.message == "纠错/翻译模型已就绪"
    missing = _check(FakeWorld(ollama=OllamaProbe(False, False, None, False, None, "x")), _registry()).ollama
    assert missing.installed is False and missing.message == "未安装用户级 Ollama"


def test_asr_device_label_and_fault_absorption_never_500():
    reg = _registry(asr=("running", None))

    # 匹配变体：CUDA GPU / Apple MPS / CPU
    cuda = _check(
        FakeWorld(weights=READY_WEIGHTS, asr_variant="cuda", asr_hardware=AsrHardwareProbe("cuda", "RTX 4070", "RTX 4070")),
        reg,
    ).asr
    assert cuda.device_label == "CUDA GPU"

    mps = _check(
        FakeWorld(weights=READY_WEIGHTS, asr_variant="mps", asr_hardware=AsrHardwareProbe("mps", "Apple Silicon")),
        reg,
    ).asr
    assert mps.device_label == "Apple MPS"

    cpu = _check(
        FakeWorld(weights=READY_WEIGHTS, asr_variant="cpu", asr_hardware=AsrHardwareProbe("cpu", "无卡")),
        reg,
    ).asr
    assert cpu.device_label == "CPU"

    # 已装 CPU 但检测到符合条件的 CUDA GPU → 明示可一键升级
    upgrade = _check(
        FakeWorld(
            weights=READY_WEIGHTS,
            asr_variant="cpu",
            asr_hardware=AsrHardwareProbe("cuda", "检测到 GPU", gpu_name="RTX 4070", driver_version="572.16"),
        ),
        reg,
    ).asr
    assert upgrade.device_label is not None and "CPU" in upgrade.device_label and "RTX 4070" in upgrade.device_label and "一键装齐" in upgrade.device_label

    # 旧驱动 < 570 或探测故障：保留在 device_label，绝不 500
    old_drv = _check(
        FakeWorld(
            weights=READY_WEIGHTS,
            asr_variant="cpu",
            asr_hardware=AsrHardwareProbe(None, "驱动过旧", error="当前驱动版本 566.36 低于 570.00"),
        ),
        reg,
    ).asr
    assert old_drv.device_label is not None and "CPU" in old_drv.device_label and "570" in old_drv.device_label

    # 清单损坏 ValueError 与硬件探测未预期异常全部吸收，永不 500
    corrupt = _check(FakeWorld(weights=READY_WEIGHTS, asr_variant=ValueError("manifest bad json")), reg).asr
    assert corrupt.device_label is not None and "清单损坏" in corrupt.device_label and "一键装齐" in corrupt.device_label

    boom = _check(FakeWorld(weights=READY_WEIGHTS, asr_variant="cpu", asr_hardware=RuntimeError("smi crashed")), reg).asr
    assert boom.device_label is not None and "CPU" in boom.device_label and "smi crashed" in boom.device_label
