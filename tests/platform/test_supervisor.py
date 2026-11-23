"""U7 看护运行器：按序拉起、就绪、退出先清会话再退避、停止分流（ollama/asr → 底栏 error 恰一条；server → 主线程托盘错误态）、
托盘重试、逆序停止、进程记录；八.10：看护线程卡在就绪探针时 Qt 主线程照常派发（真实 QCoreApplication）。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer

from _contracts import ComponentName, ComponentState
from _desktop.supervisor import Supervisor
from _desktop.ui_bridge import QtUiBridge
from _paths import AppPaths
from _supervision.machine import BACKOFF_S, MAX_CONSECUTIVE_FAILURES, UPGRADE_STOP_REASON
from _supervision.spec import ReadyResult, build_specs

NAMES = list(ComponentName)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeHandle:
    def __init__(self, log: list, name: str, pid: int) -> None:
        self.log, self.name, self.pid, self.sid, self.identity = log, name, pid, pid, f"id{pid}"
        self.exit: int | None = None
        self.stuck = False

    def poll(self):
        return self.exit

    def terminate_session(self, timeout_s: float):
        self.log.append(("terminate", self.name))
        if self.stuck:
            raise RuntimeError(f"会话 {self.pid} 在 KILL 后仍有进程")
        return [self.pid]


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.paths = AppPaths.for_roots(config_root=tmp_path / "c", data_root=tmp_path / "d", runtime_dir=tmp_path / "r")
        self.ready = {n: False for n in NAMES}
        self.port_owner: dict[int, str] = {}
        self.log: list = []
        self.handles: dict[str, FakeHandle] = {}
        self.errors: list[str] = []
        self.snapshots: list[list] = []
        self.tray: list = []
        self.clock = Clock()
        self.fail_spawn: dict[str, Exception] = {}

    def specs(self):
        out = []
        for spec in build_specs(self.paths, {}):
            name = spec.name
            out.append(replace(spec, installed=lambda: None, preflight=lambda: None,
                               ready=lambda n=name: ReadyResult(self.ready[n], None if self.ready[n] else "未响应")))
        return out

    def spawn(self, argv, *, cwd, env, log_path):
        name = next(s.name.value for s in self.specs() if tuple(argv) == s.argv)
        if name in self.fail_spawn:
            raise self.fail_spawn[name]
        self.log.append(("spawn", name))
        self.handles[name] = FakeHandle(self.log, name, 1000 + len(self.log))
        return self.handles[name]

    def supervisor(self, **overrides) -> Supervisor:
        world = self

        class Status:
            def error(self, code, message):
                world.errors.append(message)

            def set_components(self, components):
                world.snapshots.append([(c.name.value, c.state.value) for c in components])

        class Tray:
            def set_state(self, state, tooltip=None):
                world.tray.append(("state", state, tooltip))

            def set_server_stopped(self, stopped):
                world.tray.append(("server_stopped", stopped))

        kwargs = dict(specs=self.specs, records_path=self.paths.children_record, status=Status(), tray=Tray(),
                      ui=lambda fn: (world.tray.append(("ui",)), fn()), spawn=self.spawn,
                      owner_of_port=lambda port: self.port_owner.get(port), mono=self.clock, wall=lambda: 1_000_000.0)
        return Supervisor(**{**kwargs, **overrides})


def _states(sup: Supervisor) -> dict[str, str]:
    return {s.name.value: s.state.value for s in sup.snapshot()}


def test_starts_in_order_becomes_running_and_reports_changes_only(tmp_path):
    w = World(tmp_path)
    opened: list[str] = []
    sup = w.supervisor(on_server_running=lambda: opened.append("main window"))
    sup.tick()
    assert w.log == [("spawn", "ollama"), ("spawn", "asr"), ("spawn", "server")]
    assert _states(sup) == {"ollama": "starting", "asr": "starting", "server": "starting"}
    records = json.loads(w.paths.children_record.read_text(encoding="utf-8"))
    assert sorted(records["children"]) == ["asr", "ollama", "server"]
    w.ready.update(ollama=True, server=True)
    w.clock.now += 1
    sup.tick()
    sup.tick()  # 无变化不重复上报
    assert w.snapshots[-1] == [("ollama", "running"), ("asr", "starting"), ("server", "running")] and len(w.snapshots) == 2
    assert opened == ["main window"] and w.tray[-1] == ("ui",)  # 服务端第一次就绪时经主线程打开一次主窗口
    w.handles["server"].exit = 1
    w.clock.now += 1
    sup.tick()
    w.clock.now += BACKOFF_S[0]
    sup.tick()
    sup.tick()
    assert opened == ["main window"]  # 之后重启不再弹窗


def test_exit_terminates_the_session_before_backing_off(tmp_path):
    w = World(tmp_path)
    sup = w.supervisor()
    sup.tick()
    w.handles["asr"].exit = 1
    w.clock.now += 1
    sup.tick()
    assert w.log[-1] == ("terminate", "asr") and _states(sup)["asr"] == "backing_off"
    assert "asr" not in json.loads(w.paths.children_record.read_text(encoding="utf-8"))["children"]
    w.clock.now += BACKOFF_S[0] - 0.1
    sup.tick()
    assert w.log.count(("spawn", "asr")) == 1
    w.clock.now += 0.1
    sup.tick()
    assert w.log.count(("spawn", "asr")) == 2 and _states(sup)["asr"] == "starting"


def test_stopped_asr_reports_one_error_and_stopped_server_sets_tray_until_retry(tmp_path):
    w = World(tmp_path)
    w.port_owner[w.specs()[2].port] = "PID 4242（python3）"  # 端口被别的服务端占着
    sup = w.supervisor()
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        sup.tick()
        w.handles["asr"].exit = 1
        w.clock.now += 1
        sup.tick()
        w.clock.now += max(BACKOFF_S)
    sup.tick()
    assert _states(sup)["asr"] == "stopped" and _states(sup)["server"] == "stopped"
    assert len(w.errors) == 1 and w.errors[0].startswith("语音识别服务已停止：连续失败 5 次已停止：启动中退出（退出码 1）（日志 ")
    tooltip = "本地服务已停止：端口 17945 被 PID 4242（python3）占用，未抢占"
    assert w.tray == [("ui",), ("state", "error", tooltip), ("server_stopped", True)]
    assert ("spawn", "server") not in w.log

    del w.port_owner[w.specs()[2].port]
    sup.retry("server")  # 托盘「重试启动本地服务」
    sup.tick()
    assert _states(sup)["server"] == "starting" and w.log[-1] == ("spawn", "server")
    assert w.tray[-2:] == [("ui",), ("server_stopped", False)]
    assert [s for s in sup.snapshot() if s.name == ComponentName.server][0].failures == 0


def test_spawn_failure_is_a_visible_failure_and_stop_all_terminates_in_reverse(tmp_path):
    w = World(tmp_path)
    w.fail_spawn["server"] = OSError("证书目录不可写")
    sup = w.supervisor()
    sup.tick()
    server = [s for s in sup.snapshot() if s.name == ComponentName.server][0]
    assert server.state == ComponentState.backing_off and server.reason == "无法启动：OSError: 证书目录不可写"
    del w.fail_spawn["server"]
    w.clock.now += BACKOFF_S[0]
    sup.tick()
    sup.stop_all()
    assert [entry for entry in w.log if entry[0] == "terminate"] == [("terminate", "server"), ("terminate", "asr"), ("terminate", "ollama")]
    assert {s.state for s in sup.snapshot()} == {ComponentState.stopped} and {s.reason for s in sup.snapshot()} == {"应用退出"}
    assert json.loads(w.paths.children_record.read_text(encoding="utf-8"))["children"] == {}


def test_failed_session_cleanup_fails_only_that_component_and_stays_recorded(tmp_path):
    w = World(tmp_path)
    sup = w.supervisor()
    sup.tick()
    stuck = w.handles["asr"]
    stuck.exit, stuck.stuck = 1, True
    w.clock.now += 1
    sup.tick()  # 结束会话抛错：不得冒出 tick
    asr = [s for s in sup.snapshot() if s.name == ComponentName.asr][0]
    assert asr.state == ComponentState.backing_off and asr.failures == 1  # 同一次事件（退出 + 清会话失败）只计一次失败
    assert asr.reason == f"启动中退出（退出码 1）；无法结束组件进程：RuntimeError: 会话 {stuck.pid} 在 KILL 后仍有进程"
    assert _states(sup)["ollama"] == "starting" and _states(sup)["server"] == "starting"
    children = json.loads(w.paths.children_record.read_text(encoding="utf-8"))["children"]
    assert children[f"asr-{stuck.pid}"]["pid"] == stuck.pid  # 下次启动按记录再清
    w.ready.update(ollama=True)
    w.clock.now += max(BACKOFF_S)
    sup.tick()
    assert _states(sup)["ollama"] == "running" and w.log.count(("spawn", "asr")) == 2


def test_record_write_failure_fails_the_start_and_ends_the_new_session(tmp_path):
    w = World(tmp_path)
    w.paths.children_record.mkdir(parents=True)  # 记录路径被目录占着：原子写必然失败
    sup = w.supervisor()
    sup.tick()
    for status in sup.snapshot():
        assert status.state == ComponentState.backing_off and status.reason.startswith("无法写进程记录")
    assert [e for e in w.log if e[0] == "terminate"] == [("terminate", "ollama"), ("terminate", "asr"), ("terminate", "server")]
    assert w.tray[-1][:2] == ("state", "error") and w.tray[-1][2].startswith("无法写进程记录")


def test_supervisor_thread_survives_unexpected_errors_and_shows_them_on_the_tray(tmp_path):
    w = World(tmp_path)
    w.paths.children_record.parent.mkdir(parents=True)
    w.paths.children_record.write_text("{", encoding="utf-8")  # 记录损坏：启动清理失败
    calls = {"n": 0}

    def flaky_specs():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("识别运行环境清单损坏")
        return w.specs()

    sup = w.supervisor(specs=flaky_specs, mono=time.monotonic, tick_s=0.01)
    sup.start()
    try:
        deadline = time.monotonic() + 5
        while len([e for e in w.log if e[0] == "spawn"]) < 3:
            assert time.monotonic() < deadline, w.tray
            time.sleep(0.01)
        tips = [entry[2] for entry in w.tray if entry[0] == "state"]
        assert tips[0].startswith("无法清理上次运行残留的组件进程：ValueError")
        assert tips[1] == "看护出错：ValueError: 识别运行环境清单损坏"
    finally:
        sup.stop(timeout_s=10)


def test_qt_main_thread_keeps_dispatching_while_supervisor_blocks_in_ready_probe(tmp_path):
    """八.10：看护线程在就绪探针里阻塞时，经 QtUiBridge 投递的热键边沿 50ms 内执行、主线程定时器照常触发。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    w = World(tmp_path)
    in_probe, release = threading.Event(), threading.Event()

    def blocking_specs():
        specs = w.specs()
        return [replace(specs[0], ready=lambda: (in_probe.set(), release.wait(10), ReadyResult(True))[-1]), *specs[1:]]

    bridge = QtUiBridge()
    sup = w.supervisor(specs=blocking_specs, ui=bridge.post, mono=time.monotonic, wall=time.time, tick_s=0.01)
    ticks: list[float] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    watchdog = threading.Timer(3.0, release.set)  # 若探针跑在主线程，3s 后放行，免得卡死；下面的断言会因此失败
    watchdog.start()
    sup.start()
    try:
        assert in_probe.wait(5)
        latencies: list[float] = []
        for _ in range(5):
            posted = time.monotonic()
            done = threading.Event()
            threading.Thread(target=lambda: bridge.post(lambda: (latencies.append(time.monotonic() - posted), done.set()))).start()
            deadline = time.monotonic() + 2
            while not done.is_set() and time.monotonic() < deadline:
                app.processEvents()
        before = len(ticks)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            app.processEvents()
        assert in_probe.is_set() and not release.is_set()  # 全程看护线程都还卡在探针里
        assert len(latencies) == 5 and max(latencies) < 0.05, latencies
        assert len(ticks) - before >= 5  # 200ms 内 10ms 定时器至少触发 5 次
    finally:
        timer.stop()
        watchdog.cancel()
        release.set()
        sup.stop(timeout_s=10)  # 线程未结束会抛错


def test_stop_component_is_non_blocking_idempotent_and_preserves_terminate_failure(tmp_path):
    w = World(tmp_path)
    seen_timeouts: list[float] = []

    class TimeoutTrackHandle(FakeHandle):
        def terminate_session(self, timeout_s: float):
            seen_timeouts.append(timeout_s)
            return super().terminate_session(timeout_s)

    def track_spawn(argv, *, cwd, env, log_path):
        name = next(s.name.value for s in w.specs() if tuple(argv) == s.argv)
        w.log.append(("spawn", name))
        w.handles[name] = TimeoutTrackHandle(w.log, name, 2000 + len(w.log))
        return w.handles[name]

    def specs_with_custom_timeout():
        return [replace(s, stop_timeout_s=23.0) if s.name == ComponentName.asr else s for s in w.specs()]

    # 首轮前即入队 stop_component：tick() 先刷 specs 再排空命令，不拉起 asr 且不弹底栏 error
    sup0 = w.supervisor(specs=specs_with_custom_timeout, spawn=track_spawn)
    sup0.stop_component("asr")
    assert w.log == []  # 非阻塞：调用当下不终结、不拉起
    sup0.tick()
    asr0 = next(s for s in sup0.snapshot() if s.name == ComponentName.asr)
    assert (asr0.state, asr0.reason, asr0.pid) == (ComponentState.stopped, UPGRADE_STOP_REASON, None)
    assert ("spawn", "asr") not in w.log and w.errors == []

    # 正常运行中主动停服：释放会话（使用 spec.stop_timeout_s）、置 stopped 且 pid=None、无底栏 error、可经 retry 重拉起
    sup = w.supervisor(specs=specs_with_custom_timeout, spawn=track_spawn)
    sup.tick()
    w.ready.update(asr=True)
    w.clock.now += 1
    sup.tick()
    assert _states(sup)["asr"] == "running"

    sup.stop_component("asr")
    assert ("terminate", "asr") not in w.log  # 仅入队
    w.clock.now += 1
    sup.tick()
    asr = next(s for s in sup.snapshot() if s.name == ComponentName.asr)
    assert (asr.state, asr.reason, asr.pid) == (ComponentState.stopped, UPGRADE_STOP_REASON, None)
    assert seen_timeouts == [23.0] and w.errors == []
    w.clock.now += 10
    sup.tick()
    assert _states(sup)["asr"] == "stopped"  # 不会自动退避拉起

    # retry 后立刻按最新 spec 重拉起
    sup.retry("asr")
    w.clock.now += 1
    sup.tick()
    assert _states(sup)["asr"] == "starting" and w.log.count(("spawn", "asr")) == 2

    # 非退出路径 _terminate 失败：保留 pid 与「无法结束组件进程」，且后续重复 stop 幂等不覆写失败态
    stuck = w.handles["asr"]
    stuck.stuck = True
    sup.stop_component("asr")
    w.clock.now += 1
    sup.tick()
    failed = next(s for s in sup.snapshot() if s.name == ComponentName.asr)
    assert failed.state == ComponentState.stopped and failed.pid == stuck.pid
    assert failed.reason is not None and "无法结束组件进程" in failed.reason

    sup.stop_component("asr")
    w.clock.now += 2
    sup.tick()
    repeated = next(s for s in sup.snapshot() if s.name == ComponentName.asr)
    assert repeated.state == ComponentState.stopped and repeated.pid == stuck.pid
    assert repeated.reason == failed.reason


def test_supervisor_forwards_log_max_lines_only_when_configured(tmp_path):
    w = World(tmp_path)
    seen_kwargs: list[dict] = []

    def spy_spawn(argv, **kw):
        seen_kwargs.append(dict(kw))
        return FakeHandle(w.log, "x", 2000 + len(seen_kwargs))

    sup = w.supervisor(spawn=spy_spawn, log_max_lines=lambda: 500)
    sup.tick()
    assert len(seen_kwargs) == 3
    assert all(kw.get("max_lines") == 500 for kw in seen_kwargs)
