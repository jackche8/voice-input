"""真实注入后端：把已定稿文本送到当前焦点窗口，并在粘贴后无损归还用户原剪贴板（含图片）。

焦点比对在 `_inject.FocusBoundInjector`（按 job 绑定的焦点）完成，本模块负责输入与窗口级安全护栏：
1. 自窗口拦截保护：若焦点落在自身进程或 `ollama-voice-input` 浮窗，立即拒绝盲粘并返回明确原因（proved_none）。
2. 智能粘贴键选择：X11 与 Wayland 默认均发 `Ctrl+V`；仅当 `terminal=True` 或 X11/XWayland 窗口
   `WM_CLASS` 命中终端模拟器列表时发 `Ctrl+Shift+V`（根治 Wayland 硬发 `Ctrl+Shift+V` 导致浏览器/编辑器失效）。
3. 剪贴板图片无损保护：支持 `SystemClipboard.snapshot()` 快照原始图片字节与 MIME，粘贴后延迟 `0.55s` 精确写回。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from _focus import TERMINALS
from _inject import InjectEvidence
from _linux.clipboard import Clipboard, ClipboardSnapshot, SystemClipboard
from _linux.ibus import IBusCommitter
from _linux.inject_guard import InjectGuard
from _linux.modifiers import ModifierJanitor

PASTE_RESTORE_DELAY_S = 0.55
_SELF_WM_KEYWORDS = ("ollama-voice-input", "ollama_voice_input")


@dataclass(frozen=True)
class FocusedWindowInfo:
    window_id: int = 0
    wm_class: tuple[str, ...] = ()
    wm_name: str = ""
    pid: int = 0
    is_terminal: bool = False
    is_self: bool = False


def probe_x11_window_info(
    display_factory=None,
    *,
    self_pid: int | None = None,
) -> FocusedWindowInfo:
    """探测当前 X11 / XWayland 焦点顶层窗口的 WM_CLASS、标题与 _NET_WM_PID。

    在 Wayland 下若当前焦点是原生 Wayland 窗口，XWayland `get_input_focus` 返回 PointerRoot/None，
    此时安全返回空 `FocusedWindowInfo`（`is_terminal=False, is_self=False`），默认采用 `Ctrl+V`。
    """
    if display_factory is None:
        if not os.environ.get("DISPLAY"):
            return FocusedWindowInfo()
        try:
            from Xlib import display as xdisplay
        except Exception:
            return FocusedWindowInfo()
        factory = xdisplay.Display
    else:
        factory = display_factory

    try:
        dpy = factory()
    except Exception:
        return FocusedWindowInfo()

    own_pid = os.getpid() if self_pid is None else self_pid
    try:
        focus = dpy.get_input_focus().focus
        if not focus or isinstance(focus, int):
            return FocusedWindowInfo()
        root = dpy.screen().root
        pid_atom = None
        name_atom = None
        try:
            pid_atom = dpy.intern_atom("_NET_WM_PID")
            name_atom = dpy.intern_atom("_NET_WM_NAME")
        except Exception:
            pass

        window = focus
        top_id = int(getattr(focus, "id", 0) or 0)
        wm_class: tuple[str, ...] = ()
        wm_name = ""
        win_pid = 0

        for _ in range(32):
            wid = int(getattr(window, "id", 0) or 0)
            if wid:
                top_id = wid
            if not wm_class:
                try:
                    cls = window.get_wm_class()
                    if cls:
                        wm_class = tuple(str(x) for x in cls if x)
                except Exception:
                    pass
            if not wm_name:
                if name_atom:
                    try:
                        prop = window.get_full_property(name_atom, 0)
                        if prop and prop.value:
                            val = prop.value
                            wm_name = val.decode("utf-8", errors="replace") if isinstance(val, (bytes, bytearray)) else str(val)
                    except Exception:
                        pass
                if not wm_name:
                    try:
                        raw_name = window.get_wm_name()
                        if raw_name:
                            wm_name = raw_name.decode("utf-8", errors="replace") if isinstance(raw_name, (bytes, bytearray)) else str(raw_name)
                    except Exception:
                        pass
            if not win_pid and pid_atom:
                try:
                    prop = window.get_full_property(pid_atom, 0)
                    if prop and prop.value:
                        win_pid = int(prop.value[0])
                except Exception:
                    pass
            try:
                parent = window.query_tree().parent
            except Exception:
                break
            if parent is None or parent == window or parent == root:
                break
            window = parent

        lowered_classes = tuple(c.lower() for c in wm_class)
        is_terminal = any(c in TERMINALS for c in lowered_classes)
        lowered_name = wm_name.lower()
        is_self = bool(
            (win_pid and win_pid == own_pid)
            or any(any(kw in c for kw in _SELF_WM_KEYWORDS) for c in lowered_classes)
            or any(kw in lowered_name for kw in _SELF_WM_KEYWORDS)
        )
        return FocusedWindowInfo(
            window_id=top_id,
            wm_class=wm_class,
            wm_name=wm_name,
            pid=win_pid,
            is_terminal=is_terminal,
            is_self=is_self,
        )
    except Exception:
        return FocusedWindowInfo()
    finally:
        try:
            dpy.close()
        except Exception:
            pass


class _PasteInterrupted(RuntimeError):
    """已经开始发送按键后失败：不能证明没有输出。"""


class LinuxPasteBackend:
    def __init__(
        self,
        *,
        session_type: str,
        guard: InjectGuard,
        janitor: ModifierJanitor,
        clipboard: Clipboard,
        ibus: IBusCommitter | None = None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
        window_probe: Callable[[], FocusedWindowInfo] | None = None,
    ) -> None:
        self.session_type = session_type
        self.guard = guard
        self.janitor = janitor
        self.clipboard = clipboard
        self.ibus = ibus or IBusCommitter()
        self._run = run
        self._sleep = sleep
        self._xdotool = which("xdotool")
        self._wtype = which("wtype")
        if window_probe is not None:
            self._window_probe = window_probe
        elif isinstance(clipboard, SystemClipboard):
            self._window_probe = probe_x11_window_info
        else:
            self._window_probe = FocusedWindowInfo

    # ------------------------------------------------------------------
    def deliver(self, text: str, *, terminal: bool = False) -> InjectEvidence:
        """全程持有注入锁（与重复输入/看门狗互斥），注入期间打开回流保护窗。"""
        with self.guard.lock:
            self.guard.open_window(3.0)  # 注入期间抓取器忽略回流按键
            try:
                self._sleep(0.1)  # 等待焦点稳定
                info = self._window_probe()
                if info.is_self:
                    detail = f"当前焦点位于自身窗口 ({info.wm_class or info.wm_name or info.pid})，已拦截盲粘"
                    print(f"  输入: [BLOCKED] {detail}", flush=True)
                    return InjectEvidence(False, False, True, method="self_window_blocked", detail=detail)
                effective_terminal = bool(terminal or info.is_terminal)
                if self.ibus.commit(text):
                    print(f"  输入: Tier1 IBus 直接提交成功 ({len(text)} 字)", flush=True)
                    return InjectEvidence(True, True, False, method="ibus", detail="已输入")
                return self._paste_with_restore(text, terminal=effective_terminal)
            finally:
                self.guard.open_window(0.2)  # 注入尾窗，覆盖回流事件延迟

    def _take_snapshot(self) -> ClipboardSnapshot | str:
        snap_fn = getattr(self.clipboard, "snapshot", None)
        if callable(snap_fn):
            return snap_fn()
        return self.clipboard.read()

    def _restore(self, saved: ClipboardSnapshot | str, injected_text: str) -> None:
        if self.clipboard.read() != injected_text:
            return
        if isinstance(saved, ClipboardSnapshot):
            restore_fn = getattr(self.clipboard, "restore_snapshot", None)
            if callable(restore_fn):
                restore_fn(saved)
                return
            self.clipboard.write(saved.text if saved.kind == "text" else "")
            return
        self.clipboard.write(saved)

    def _paste_with_restore(self, text: str, *, terminal: bool) -> InjectEvidence:
        """快照原剪贴板（含 image/png）→ 写入文本 → 粘贴 → 延迟 0.55s 后仅在剪贴板未变时无损恢复。"""
        saved = self._take_snapshot()
        error = self._write_verified(text)
        if error is not None:
            return InjectEvidence(False, False, True, method="clipboard", detail=error)
        try:
            method = self._send_paste(terminal=terminal)
        except _PasteInterrupted as exc:
            self._restore(saved, text)
            return InjectEvidence(False, False, False, method="paste", detail=str(exc))
        if method is None:
            self._restore(saved, text)
            print("  输入: [ERROR] 所有输入方式均不可用！", flush=True)
            return InjectEvidence(
                False, False, True, method="none", detail="无法模拟粘贴：xdotool/uinput/pynput 均不可用"
            )
        self._sleep(PASTE_RESTORE_DELAY_S)
        self._restore(saved, text)
        return InjectEvidence(True, True, False, method=method, detail="已发送粘贴")

    def _write_verified(self, text: str) -> str | None:
        error = self.clipboard.write(text)
        if error is not None:
            return error
        # xclip → X11 CLIPBOARD → Mutter 桥接 → Wayland 剪贴板，需要足够延迟
        self._sleep(0.3)
        check = self.clipboard.read()
        if check == text:
            print(f"  输入: 剪贴板已写入 ({len(text)} 字) ✓", flush=True)
            return None
        print(
            f"  输入: [WARN] 剪贴板验证失败! 期望 {len(text)} 字, 实际 {len(check) if check else 0} 字",
            flush=True,
        )
        error = self.clipboard.write(text)
        if error is not None:
            return f"剪贴板重试写入失败：{error}"
        self._sleep(0.3)
        return None

    # ------------------------------------------------------------------
    def _send_paste(self, *, terminal: bool) -> str | None:
        """返回成功的粘贴方式名；一个键都没发出返回 None；发送途中失败抛 _PasteInterrupted。"""
        print(f"  输入: 使用剪贴板粘贴 (终端={terminal}, 会话={self.session_type})", flush=True)
        self.janitor.wait_released()
        if self.session_type == "wayland":
            # Wayland 默认发送标准 Ctrl+V；仅在探测到终端模拟器时发送 Ctrl+Shift+V
            if self._uinput_paste(terminal):
                return "uinput"
            if self._wtype:
                wtype_cmd = (
                    [self._wtype, "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
                    if terminal
                    else [self._wtype, "-M", "ctrl", "-k", "v", "-m", "ctrl"]
                )
                if self._run_ok(wtype_cmd):
                    return "wtype"
        else:
            if self._uinput_paste(terminal):
                return "uinput"
            if self._xdotool:
                combo = "ctrl+shift+v" if terminal else "ctrl+v"
                if self._run_ok([self._xdotool, "key", combo]):
                    return "xdotool"
        return self._pynput_paste(terminal)

    def _run_ok(self, cmd: list[str]) -> bool:
        try:
            r = self._run(cmd, timeout=3, check=False, capture_output=True)
        except Exception as exc:
            print(f"  输入: {cmd[0]} 异常 {exc}, 回退下一级", flush=True)
            return False
        if r.returncode != 0:
            print(f"  输入: {cmd[0]} 返回码 {r.returncode}, 回退下一级", flush=True)
            return False
        return True

    def _uinput_paste(self, is_term: bool) -> bool:
        """用 uinput 合成 Ctrl(+Shift)+V 粘贴，成功返回 True。"""
        uinput = self.janitor.uinput
        if not uinput:
            return False
        from evdev import ecodes

        seq: list[int] = [ecodes.KEY_LEFTCTRL]
        if is_term:
            seq.append(ecodes.KEY_LEFTSHIFT)
        seq.append(ecodes.KEY_V)
        pressed: list[int] = []
        ok = False
        try:
            for code in seq:
                uinput.write(ecodes.EV_KEY, code, 1)
                pressed.append(code)
            uinput.syn()
            self._sleep(0.03)
            uinput.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
            ok = True
        except Exception as exc:
            print(f"  输入: uinput 粘贴失败: {exc}", flush=True)
            if pressed:
                raise _PasteInterrupted(f"uinput 粘贴中途失败: {exc}") from exc
        finally:
            for code in reversed(pressed):
                try:
                    uinput.write(ecodes.EV_KEY, code, 0)
                except Exception:
                    pass
            try:
                uinput.syn()
            except Exception as exc:
                print(f"  输入: uinput release syn 失败: {exc}", flush=True)
            self.janitor.clear_uinput()
        return ok

    def _pynput_paste(self, terminal: bool) -> str | None:
        kb, Key = self.janitor.kb, self.janitor.Key
        if kb is None:
            return None
        kb_lock = getattr(self.janitor, "kb_lock", None)
        guard_ctx = kb_lock if kb_lock is not None else contextlib.nullcontext()
        pressed_keys: list = [Key.ctrl]
        if terminal:
            pressed_keys.append(Key.shift)
        held: list = []
        with guard_ctx:
            try:
                for k in pressed_keys:
                    kb.press(k)
                    held.append(k)
                kb.tap("v")
            except Exception as exc:
                print(f"  输入: Tier4 pynput 失败: {exc}", flush=True)
                if held:
                    raise _PasteInterrupted(f"pynput 粘贴中途失败: {exc}") from exc
                return None
            finally:
                for k in reversed(held):
                    try:
                        kb.release(k)
                    except Exception:
                        pass
                self.janitor.clear_pynput()
        return "pynput"
