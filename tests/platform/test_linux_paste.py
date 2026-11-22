"""粘贴后端、图片剪贴板无损恢复、IBus 私有总线探测、Wayland Ctrl+V/终端识别、自窗口拦截与音频/修饰键锁测试。"""
from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path

from _inject import ack_from_evidence
from _linux.clipboard import SystemClipboard
from _linux.ibus import IBusCommitter, probe_ibus_commit_support, probe_ibus_private_bus, resolve_ibus_address
from _linux.inject_guard import InjectGuard
from _linux.mic import play_beep
from _linux.modifiers import ModifierJanitor
from _linux.paste_backend import (
    PASTE_RESTORE_DELAY_S,
    FocusedWindowInfo,
    LinuxPasteBackend,
    probe_x11_window_info,
)


class FakeClipboard:
    """测试专用纯文本剪贴板：记录读写；可让写失败或在粘贴后被“用户”改写。"""

    def __init__(self, initial: str = "用户原剪贴板") -> None:
        self.value = initial
        self.writes: list[str] = []
        self.fail_write: str | None = None
        self.on_paste_user_copies: str | None = None

    def write(self, text: str) -> str | None:
        if self.fail_write is not None:
            return self.fail_write
        self.writes.append(text)
        self.value = text
        return None

    def read(self) -> str:
        return self.value


class FakeJanitor:
    uinput = None
    kb = None
    Key = None

    def __init__(self) -> None:
        self.waited = 0
        self.cleared: list[str] = []
        self.kb_lock = threading.RLock()

    def wait_released(self) -> None:
        self.waited += 1

    def clear_uinput(self) -> None:
        self.cleared.append("uinput")

    def clear_pynput(self) -> None:
        self.cleared.append("pynput")


class FakeKey:
    ctrl = "ctrl"
    ctrl_r = "ctrl_r"
    shift = "shift"
    shift_r = "shift_r"
    alt = "alt"
    alt_r = "alt_r"
    cmd = "cmd"
    cmd_r = "cmd_r"


class FakeKb:
    def __init__(self, fail_on_tap: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail_on_tap = fail_on_tap

    def press(self, k):
        self.events.append(("press", k))

    def release(self, k):
        self.events.append(("release", k))

    def tap(self, k):
        if self.fail_on_tap:
            raise RuntimeError("tap boom")
        self.events.append(("tap", k))


def _backend(clip, *, session="x11", run=None, which=None, janitor=None, window_probe=None, sleeps=None):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        del kw
        calls.append(cmd)
        if getattr(clip, "on_paste_user_copies", None) is not None:
            clip.value = clip.on_paste_user_copies
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = LinuxPasteBackend(
        session_type=session,
        guard=InjectGuard(),
        janitor=janitor or FakeJanitor(),
        clipboard=clip,
        run=run or fake_run,
        which=which or (lambda name: f"/usr/bin/{name}" if name == "xdotool" else None),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        window_probe=window_probe,
    )
    return backend, calls


def test_xdotool_paste_complete_and_clipboard_restored():
    clip = FakeClipboard()
    sleeps: list[float] = []
    backend, calls = _backend(clip, sleeps=sleeps)
    ev = backend.deliver("你好")
    assert ev.method == "xdotool" and ev.proved_output
    assert ack_from_evidence(ev) == "complete"
    assert calls == [["/usr/bin/xdotool", "key", "ctrl+v"]]
    assert PASTE_RESTORE_DELAY_S == 0.55
    assert 0.55 in sleeps
    assert clip.value == "用户原剪贴板"
    assert clip.writes == ["你好", "用户原剪贴板"]


def test_terminal_uses_ctrl_shift_v():
    clip = FakeClipboard()
    backend, calls = _backend(clip)
    backend.deliver("ls", terminal=True)
    assert calls == [["/usr/bin/xdotool", "key", "ctrl+shift+v"]]


def test_user_copy_during_paste_not_overwritten():
    clip = FakeClipboard()
    clip.on_paste_user_copies = "用户新复制"
    backend, _ = _backend(clip)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "complete"
    assert clip.value == "用户新复制"


def test_clipboard_write_failure_proves_none():
    clip = FakeClipboard()
    clip.fail_write = "xclip 不可用"
    backend, calls = _backend(clip)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "none"
    assert calls == []


def test_no_channel_proves_none_and_restores():
    clip = FakeClipboard()
    backend, _ = _backend(clip, which=lambda name: None)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "none"
    assert clip.value == "用户原剪贴板"


def test_midway_failure_is_partial_or_unknown_not_retried():
    clip = FakeClipboard()
    janitor = FakeJanitor()
    janitor.kb = FakeKb(fail_on_tap=True)
    janitor.Key = FakeKey
    backend, _calls = _backend(clip, which=lambda name: None, janitor=janitor)
    ev = backend.deliver("你好")
    assert ack_from_evidence(ev) == "partial_or_unknown"
    assert ("release", "ctrl") in janitor.kb.events
    assert "pynput" in janitor.cleared
    assert clip.value == "用户原剪贴板"


def test_wayland_defaults_to_ctrl_v_and_terminal_uses_ctrl_shift_v():
    """根治验证：Wayland 默认必须发 Ctrl+V；仅终端窗口发 Ctrl+Shift+V。"""
    clip = FakeClipboard()
    backend_gui, calls_gui = _backend(
        clip,
        session="wayland",
        which=lambda name: f"/usr/bin/{name}" if name == "wtype" else None,
        window_probe=lambda: FocusedWindowInfo(window_id=0, is_terminal=False, is_self=False),
    )
    ev_gui = backend_gui.deliver("浏览器输入")
    assert ev_gui.method == "wtype" and ev_gui.proved_output
    assert calls_gui == [["/usr/bin/wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]]

    backend_term, calls_term = _backend(
        clip,
        session="wayland",
        which=lambda name: f"/usr/bin/{name}" if name == "wtype" else None,
        window_probe=lambda: FocusedWindowInfo(window_id=101, wm_class=("kitty", "kitty"), is_terminal=True, is_self=False),
    )
    ev_term = backend_term.deliver("echo ok")
    assert ev_term.method == "wtype" and ev_term.proved_output
    assert calls_term == [["/usr/bin/wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]]


def test_self_window_focus_blocks_blind_injection():
    """根治验证：焦点落在自身进程或 ollama-voice-input 浮窗时拒绝盲粘（proved_none）。"""
    ibus_called = []

    class FakeIBus:
        def commit(self, text):
            ibus_called.append(text)
            return True

    clip = FakeClipboard()
    backend, calls = _backend(
        clip,
        window_probe=lambda: FocusedWindowInfo(
            window_id=99, wm_class=("ollama-voice-input", "OllamaVoiceInput"), is_self=True
        ),
    )
    backend.ibus = FakeIBus()
    ev = backend.deliver("绝不能粘进自己的浮窗")
    assert ev.method == "self_window_blocked"
    assert ack_from_evidence(ev) == "none"
    assert ibus_called == []
    assert clip.writes == []
    assert calls == []


def test_probe_x11_window_info_terminal_and_self_detection():
    class FakeProp:
        def __init__(self, value):
            self.value = value

    class FakeWin:
        def __init__(self, wid, wm_class=None, wm_name="", pid=0, parent=None):
            self.id = wid
            self._cls = wm_class
            self._name = wm_name
            self._pid = pid
            self._parent = parent

        def get_wm_class(self):
            return self._cls

        def get_wm_name(self):
            return self._name

        def get_full_property(self, atom, _prop_type):
            if atom == 10 and self._pid:
                return FakeProp([self._pid])
            if atom == 11 and self._name:
                return FakeProp(self._name.encode("utf-8"))
            return None

        def query_tree(self):
            return type("Tree", (), {"parent": self._parent})()

    root = FakeWin(1)

    class FakeDisplay:
        def __init__(self, focus_win):
            self._focus = focus_win

        def get_input_focus(self):
            return type("Focus", (), {"focus": self._focus})()

        def screen(self):
            return type("Screen", (), {"root": root})()

        def intern_atom(self, name):
            return {"_NET_WM_PID": 10, "_NET_WM_NAME": 11}.get(name, 0)

        def close(self):
            pass

    term_top = FakeWin(42, wm_class=("Alacritty", "Alacritty"), wm_name="zsh", pid=9999, parent=root)
    term_child = FakeWin(43, parent=term_top)
    info_term = probe_x11_window_info(lambda: FakeDisplay(term_child), self_pid=1234)
    assert info_term.window_id == 42
    assert info_term.is_terminal is True
    assert info_term.is_self is False

    self_win = FakeWin(77, wm_class=("python3", "Python3"), wm_name="Status", pid=1234, parent=root)
    info_self = probe_x11_window_info(lambda: FakeDisplay(self_win), self_pid=1234)
    assert info_self.is_self is True
    assert info_self.is_terminal is False


def test_image_clipboard_preserved_losslessly_with_055s_delay():
    """根治验证：原剪贴板为纯截图 image/png 时不盲读 UTF8，粘贴后延迟 0.55s 精确写回原 PNG 字节！"""
    original_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR_SCREENSHOT_PAYLOAD"
    state = {"mime": "image/png", "data": original_png}
    run_cmds: list[list[str]] = []
    popen_cmds: list[tuple[list[str], bytes]] = []
    sleeps: list[float] = []

    class FakePopenProc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.stdin = io.BytesIO()
            self.returncode = 0

        def communicate(self, payload=b"", timeout=None):
            del timeout
            popen_cmds.append((self.cmd, bytes(payload)))
            if "-t" in self.cmd:
                state["mime"] = self.cmd[self.cmd.index("-t") + 1]
                state["data"] = bytes(payload)
            else:
                state["mime"] = "UTF8_STRING"
                state["data"] = bytes(payload)
            return b"", b""

    def fake_clip_run(cmd, **kw):
        del kw
        run_cmds.append(cmd)
        if cmd == ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]:
            if state["mime"] == "image/png":
                return subprocess.CompletedProcess(cmd, 0, b"TARGETS\nTIMESTAMP\nimage/png\n", b"")
            return subprocess.CompletedProcess(cmd, 0, b"TARGETS\nUTF8_STRING\nSTRING\n", b"")
        if cmd == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, state["data"], b"")
        if cmd == ["xclip", "-selection", "clipboard", "-o"]:
            if state["mime"] == "image/png":
                return subprocess.CompletedProcess(cmd, 1, b"", b"Error: target STRING not available")
            return subprocess.CompletedProcess(cmd, 0, state["data"], b"")
        raise AssertionError(f"unexpected clipboard cmd: {cmd}")

    sys_clip = SystemClipboard(
        which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        environ={"XDG_SESSION_TYPE": "x11"},
        popen=lambda cmd, **kw: FakePopenProc(cmd),
        run=fake_clip_run,
    )
    paste_cmds: list[list[str]] = []
    backend = LinuxPasteBackend(
        session_type="x11",
        guard=InjectGuard(),
        janitor=FakeJanitor(),
        clipboard=sys_clip,
        ibus=IBusCommitter(None, available=False),
        run=lambda cmd, **kw: (paste_cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")),
        which=lambda name: "/usr/bin/xdotool" if name == "xdotool" else None,
        sleep=sleeps.append,
        window_probe=lambda: FocusedWindowInfo(window_id=10, wm_class=("code", "Code")),
    )

    ev = backend.deliver("语音转写内容")
    assert ev.proved_output and ack_from_evidence(ev) == "complete"
    # 快照阶段绝未盲调 `xclip -selection clipboard -o`
    assert run_cmds[0] == ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
    assert run_cmds[1] == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    assert paste_cmds == [["/usr/bin/xdotool", "key", "ctrl+v"]]
    assert sleeps[-1] == 0.55
    # 恢复后剪贴板完整回到原始 image/png 字节
    assert state == {"mime": "image/png", "data": original_png}
    assert popen_cmds[-1] == (["xclip", "-selection", "clipboard", "-t", "image/png"], original_png)


def test_wayland_wl_paste_image_snapshot_and_restore():
    raw_png = b"\x89PNG_WAYLAND_IMAGE"
    state = {"types": "image/png\n", "png": raw_png, "text": ""}

    def fake_run(cmd, **kw):
        del kw
        if cmd == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, state["types"].encode(), b"")
        if cmd == ["wl-paste", "--type", "image/png"]:
            return subprocess.CompletedProcess(cmd, 0, state["png"], b"")
        if cmd == ["wl-paste", "--no-newline"]:
            return subprocess.CompletedProcess(cmd, 0, state["text"].encode(), b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    restored: list[tuple[list[str], bytes]] = []

    class Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.returncode = 0

        def communicate(self, payload=b"", timeout=None):
            del timeout
            restored.append((self.cmd, bytes(payload)))
            return b"", b""

    clip = SystemClipboard(
        which=lambda n: f"/usr/bin/{n}" if n in {"wl-paste", "wl-copy"} else None,
        environ={"XDG_SESSION_TYPE": "wayland"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=fake_run,
    )
    snap = clip.snapshot()
    assert snap.kind == "binary" and snap.mime == "image/png" and snap.data == raw_png
    assert clip.restore_snapshot(snap) is None
    assert restored[-1] == (["wl-copy", "--type", "image/png"], raw_png)


def _which_xclip_and_wl(name: str) -> str | None:
    return f"/usr/bin/{name}" if name in {"xclip", "wl-paste", "wl-copy"} else None


def test_wayland_text_snapshot_prefers_xclip_without_wl_paste():
    """Wayland 共存 xclip/wl-paste 时，文本剪贴板 snapshot() 只调 xclip，零 wl-paste 调用。"""
    run_cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        del kw
        run_cmds.append(cmd)
        if cmd == ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, b"TARGETS\nUTF8_STRING\n", b"")
        if cmd == ["xclip", "-selection", "clipboard", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, "Wayland 原文本".encode("utf-8"), b"")
        if cmd == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, b"text/plain;charset=utf-8\n", b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    clip = SystemClipboard(which=_which_xclip_and_wl, environ={"XDG_SESSION_TYPE": "wayland"}, run=fake_run)
    snap = clip.snapshot()
    assert snap.kind == "text" and snap.text == "Wayland 原文本"
    assert run_cmds == [
        ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
        ["xclip", "-selection", "clipboard", "-o"],
    ]
    assert all(cmd[0] != "wl-paste" for cmd in run_cmds)


def test_wayland_image_snapshot_and_restore_prefer_xclip_without_wl_clipboard():
    """Wayland 共存 xclip/wl-clipboard 时，图片剪贴板快照与恢复全走 xclip，零 wl-paste/wl-copy 调用。"""
    raw_png = b"\x89PNG\r\n\x1a\nWAYLAND_XCLIP_IMAGE"
    run_cmds: list[list[str]] = []
    popen_cmds: list[tuple[list[str], bytes]] = []

    def fake_run(cmd, **kw):
        del kw
        run_cmds.append(cmd)
        if cmd == ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, b"TARGETS\nimage/png\n", b"")
        if cmd == ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]:
            return subprocess.CompletedProcess(cmd, 0, raw_png, b"")
        if cmd == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, b"image/png\n", b"")
        if cmd == ["wl-paste", "--type", "image/png"]:
            return subprocess.CompletedProcess(cmd, 0, raw_png, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    class Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.returncode = 0

        def communicate(self, payload=b"", timeout=None):
            del timeout
            popen_cmds.append((self.cmd, bytes(payload)))
            return b"", b""

    clip = SystemClipboard(
        which=_which_xclip_and_wl,
        environ={"XDG_SESSION_TYPE": "wayland"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=fake_run,
    )
    snap = clip.snapshot()
    assert snap.kind == "binary" and snap.mime == "image/png" and snap.data == raw_png
    assert clip.restore_snapshot(snap) is None
    assert run_cmds == [
        ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ]
    assert popen_cmds == [(["xclip", "-selection", "clipboard", "-t", "image/png"], raw_png)]
    assert all(cmd[0] != "wl-paste" for cmd in run_cmds)
    assert all(cmd[0] != "wl-copy" for cmd, _ in popen_cmds)


def test_wayland_empty_clipboard_stops_at_xclip_without_wl_paste():
    """Wayland 共存时，xclip 返回空剪贴板提示或 rc=0 空输出均须立即返 empty，严禁跌落 wl-paste。"""
    cases = [
        subprocess.CompletedProcess(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            1,
            b"",
            b"Error: target TARGETS not available\n",
        ),
        subprocess.CompletedProcess(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            0,
            b"\n",
            b"",
        ),
    ]
    for xclip_res in cases:
        run_cmds: list[list[str]] = []

        def fake_run(cmd, *, _res=xclip_res, _log=run_cmds, **kw):
            del kw
            _log.append(cmd)
            if cmd[0] == "xclip":
                return _res
            return subprocess.CompletedProcess(cmd, 1, b"", b"Nothing is copied\n")

        clip = SystemClipboard(which=_which_xclip_and_wl, environ={"XDG_SESSION_TYPE": "wayland"}, run=fake_run)
        snap = clip.snapshot()
        assert snap.kind == "empty"
        assert run_cmds == [["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]]
        assert all(cmd[0] != "wl-paste" for cmd in run_cmds)


def test_wayland_read_binary_empty_or_missing_target_stops_at_xclip_without_wl_paste():
    """Wayland 共存时，read_binary 遇 xclip rc=0 空输出或目标不可用/空剪贴板提示必须立即返 b''，严禁跌落 wl-paste。"""
    xclip_cmd = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    cases = [
        subprocess.CompletedProcess(xclip_cmd, 0, b"", b""),
        subprocess.CompletedProcess(xclip_cmd, 1, b"", b"Error: target image/png not available\n"),
        subprocess.CompletedProcess(xclip_cmd, 1, b"", b"Nothing is copied\n"),
    ]
    for xclip_res in cases:
        run_cmds: list[list[str]] = []

        def fake_run(cmd, *, _res=xclip_res, _log=run_cmds, **kw):
            del kw
            _log.append(cmd)
            if cmd[0] == "xclip":
                return _res
            return subprocess.CompletedProcess(cmd, 0, b"\x89PNG_SHOULD_NOT_FALLBACK", b"")

        clip = SystemClipboard(which=_which_xclip_and_wl, environ={"XDG_SESSION_TYPE": "wayland"}, run=fake_run)
        assert clip.read_binary("image/png") == b""
        assert run_cmds == [xclip_cmd]
        assert all(cmd[0] != "wl-paste" for cmd in run_cmds)


def test_wayland_falls_back_to_wl_paste_when_xclip_cannot_open_display():
    """纯 Wayland 无 XWayland（xclip 报 Can't open display）时回退 wl-paste --list-types 与 --type image/png。"""
    raw_png = b"\x89PNG\r\n\x1a\nPURE_WAYLAND_FALLBACK"
    run_cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        del kw
        run_cmds.append(cmd)
        if cmd[0] == "xclip":
            return subprocess.CompletedProcess(cmd, 1, b"", b"Error: Can't open display: (null)\n")
        if cmd == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, b"image/png\n", b"")
        if cmd == ["wl-paste", "--type", "image/png"]:
            return subprocess.CompletedProcess(cmd, 0, raw_png, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    clip = SystemClipboard(which=_which_xclip_and_wl, environ={"XDG_SESSION_TYPE": "wayland"}, run=fake_run)
    snap = clip.snapshot()
    assert snap.kind == "binary" and snap.mime == "image/png" and snap.data == raw_png
    assert run_cmds == [
        ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
        ["wl-paste", "--list-types"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        ["wl-paste", "--type", "image/png"],
    ]


def test_ibus_private_bus_probe_and_wayland_bus_file_fallback(tmp_path: Path):
    """根治验证：IBus 通过私有总线地址探测 CurrentInputContext，不调 gdbus introspect --session。"""
    sock_file = tmp_path / "ibus-wayland.sock"
    sock_file.write_text("", encoding="utf-8")
    bus_dir = tmp_path / "bus"
    bus_dir.mkdir()
    (bus_dir / "machine-unix-0").write_text("IBUS_ADDRESS=\nIBUS_DAEMON_PID=\n", encoding="utf-8")
    expected_addr = f"unix:path={sock_file},guid=1234567890abcdef"
    (bus_dir / "machine-unix-wayland-0").write_text(
        f"IBUS_ADDRESS={expected_addr}\nIBUS_DAEMON_PID=4321\n", encoding="utf-8"
    )

    subproc_calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        del kw
        subproc_calls.append(cmd)
        # 模拟 GDK_BACKEND=x11 下 `ibus address` 返回空
        return subprocess.CompletedProcess(cmd, 0, "\n", "")

    addr = resolve_ibus_address(
        environ={"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
        which=lambda name: "/usr/bin/ibus" if name == "ibus" else "/usr/bin/gdbus",
        run=fake_run,
        bus_dir=bus_dir,
    )
    assert addr == expected_addr
    assert all("gdbus" not in c[0] for c in subproc_calls)

    class FakePrivateConn:
        def __init__(self, ctx_or_err):
            self.ctx_or_err = ctx_or_err
            self.closed = False

        def call_current_input_context(self):
            if isinstance(self.ctx_or_err, Exception):
                raise self.ctx_or_err
            return self.ctx_or_err

        def close_sync(self, _cancellable):
            self.closed = True

    conn_ok = FakePrivateConn("/org/freedesktop/IBus/InputContext_7")
    ok, note = probe_ibus_commit_support(
        bus=type("B", (), {"is_connected": lambda self: True})(),
        environ={"XDG_SESSION_TYPE": "wayland"},
        which=lambda name: "/usr/bin/ibus" if name == "ibus" else "/usr/bin/gdbus",
        run=fake_run,
        bus_dir=bus_dir,
        connection_factory=lambda a: (assert_eq(a, expected_addr) or conn_ok),
    )
    assert ok is True and "InputContext_7" in note and conn_ok.closed is True

    # 启动瞬间无焦点输入框时返回 "Bus has no focus"，仍应判为支持
    ok_nofocus, _ = probe_ibus_private_bus(
        expected_addr,
        connection_factory=lambda _a: FakePrivateConn(RuntimeError("GDBus.Error:org.freedesktop.DBus.Error.Failed: Bus has no focus.")),
    )
    assert ok_nofocus is True

    # 真正拒绝访问时返回 False
    ok_denied, reason_denied = probe_ibus_private_bus(
        expected_addr,
        connection_factory=lambda _a: FakePrivateConn(RuntimeError("org.freedesktop.DBus.Error.AccessDenied: denied")),
    )
    assert ok_denied is False and "AccessDenied" in reason_denied


def assert_eq(left, right):
    assert left == right
    return None


def test_ibus_commit_short_circuits_clipboard():
    class FakeIBus:
        def commit(self, text):
            return True

    clip = FakeClipboard()
    backend, calls = _backend(clip)
    backend.ibus = FakeIBus()
    ev = backend.deliver("你好")
    assert ev.method == "ibus" and ack_from_evidence(ev) == "complete"
    assert clip.writes == [] and calls == []


def test_inject_guard_window_open_during_delivery():
    clip = FakeClipboard()
    seen = {}

    def run(cmd, **kw):
        del kw
        seen["active"] = backend.guard.active()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend, _ = _backend(clip, run=run)
    backend.deliver("x")
    assert seen["active"] is True


def test_modifier_janitor_wayland_evdev_probe_and_kb_lock():
    held = {"down": True}
    janitor = ModifierJanitor(
        session_type="wayland",
        kb=FakeKb(),
        key=FakeKey,
        xdotool="",
        evdev_any_modifier_down=lambda: held["down"],
    )
    assert janitor.any_modifier_down() is True
    ticks = [0.0]

    def fake_sleep(dt_s: float) -> None:
        ticks[0] += dt_s
        if ticks[0] >= 0.04:
            held["down"] = False

    janitor.wait_released(timeout=0.5, sleep=fake_sleep, clock=lambda: ticks[0])
    assert held["down"] is False
    assert ticks[0] >= 0.04


def test_play_beep_serializes_concurrent_calls():
    active = 0
    max_active = 0
    calls = 0
    guard = threading.Lock()

    class FakeSd:
        def stop(self):
            pass

        def play(self, _wave, _sr):
            nonlocal active, max_active, calls
            with guard:
                active += 1
                calls += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with guard:
                active -= 1

        def wait(self):
            pass

    fake_sd = FakeSd()
    threads = [
        threading.Thread(target=play_beep, kwargs={"duration": 0.02, "background": False, "sounddevice_mod": fake_sd})
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls == 4
    assert max_active == 1


def test_linux_clipboard_protects_unsupported_and_failed_snapshots():
    """根治回归：文件列表 `text/uri-list`、未知二进制或探测失败均不得写 `""` 破坏剪贴板。"""
    written: list[tuple[list[str], bytes]] = []

    class Proc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.returncode = 0

        def communicate(self, payload=b"", timeout=None):
            del timeout
            written.append((self.cmd, bytes(payload)))
            return b"", b""

    # 1. text/uri-list（哪怕同时存在 UTF8_STRING）→ kind="unsupported"，恢复时不写空
    clip_files = SystemClipboard(
        which=lambda n: f"/usr/bin/{n}" if n == "xclip" else None,
        environ={"XDG_SESSION_TYPE": "x11"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, b"TARGETS\nx-special/gnome-copied-files\ntext/uri-list\nUTF8_STRING\n", b""
        ),
    )
    snap_files = clip_files.snapshot()
    assert snap_files.kind == "unsupported"
    assert clip_files.restore_snapshot(snap_files) is None
    assert written == []

    # 2. 非文本非图片未知 MIME（如 application/pdf）→ kind="unsupported"，恢复时不写空
    clip_pdf = SystemClipboard(
        which=lambda n: f"/usr/bin/{n}" if n == "xclip" else None,
        environ={"XDG_SESSION_TYPE": "x11"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"TARGETS\napplication/pdf\n", b""),
    )
    snap_pdf = clip_pdf.snapshot()
    assert snap_pdf.kind == "unsupported"
    assert clip_pdf.restore_snapshot(snap_pdf) is None
    assert written == []

    # 3. 探测失败（Can't open display）→ kind="failed"，恢复时不写空
    clip_fail = SystemClipboard(
        which=lambda n: f"/usr/bin/{n}" if n == "xclip" else None,
        environ={"XDG_SESSION_TYPE": "x11"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"Error: Can't open display"),
    )
    snap_fail = clip_fail.snapshot()
    assert snap_fail.kind == "failed"
    assert clip_fail.restore_snapshot(snap_fail) is None
    assert written == []

    # 4. 确认剪贴板确实为空（Nothing is copied）→ kind="empty"，恢复时允许写回空串
    clip_empty = SystemClipboard(
        which=lambda n: f"/usr/bin/{n}" if n in {"wl-paste", "wl-copy"} else None,
        environ={"XDG_SESSION_TYPE": "wayland"},
        popen=lambda cmd, **kw: Proc(cmd),
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"Nothing is copied\n"),
    )
    snap_empty = clip_empty.snapshot()
    assert snap_empty.kind == "empty"
    assert clip_empty.restore_snapshot(snap_empty) is None
    assert written == [(["wl-copy"], b"")]


def test_ibus_connect_overwrites_stale_ibus_address_env(tmp_path: Path, monkeypatch):
    """根治回归：环境变量 `IBUS_ADDRESS` 指向已失效旧 socket 时，必须用新探测到的有效地址覆盖更新。"""
    import os

    stale_sock = tmp_path / "dead-old.sock"  # 不存在 → 已失效
    live_sock = tmp_path / "live-wayland.sock"
    live_sock.write_text("", encoding="utf-8")
    bus_dir = tmp_path / "bus"
    bus_dir.mkdir()
    live_addr = f"unix:path={live_sock},guid=abcdef0123456789"
    (bus_dir / "machine-unix-wayland-0").write_text(f"IBUS_ADDRESS={live_addr}\n", encoding="utf-8")

    monkeypatch.setenv("IBUS_ADDRESS", f"unix:path={stale_sock},guid=00000000")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")

    seen_addr: list[str | None] = []

    class FakeBus:
        def is_connected(self):
            return True

    class FakeConn:
        def call_current_input_context(self):
            return "/org/freedesktop/IBus/InputContext_1"

        def close_sync(self, _c):
            pass

    committer = IBusCommitter.connect(
        which=lambda _n: None,
        bus_dir=bus_dir,
        bus_factory=lambda a: (seen_addr.append(a) or FakeBus()),
        connection_factory=lambda _a: FakeConn(),
    )
    assert committer.available is True
    assert seen_addr == [live_addr]
    assert os.environ["IBUS_ADDRESS"] == live_addr
