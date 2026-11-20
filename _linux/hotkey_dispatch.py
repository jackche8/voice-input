"""快捷键分发器（业务）：接收各后端的上报，交给唯一的按住状态判定，在 Qt 主线程执行动作。

后端（evdev / 门户 / X11 / XInput）只上报按下区间、放行、读线程进度、Esc 与事件源失败；是否开始/停止录音、
是否触发离散动作只由 `_linux/hotkey_state.HotkeyPressState` 决定。事件源失败与组件失败走同一个 on_error 通道。
规则见 docs/plans/v3.3-overlay-voice-hotkey-plan.md 8.6-F / 8.6-G。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from _linux.hotkey_actions import GLib, HotkeyContext
from _linux.hotkey_state import Action, Edge, HotkeyPressState


class HotkeyDispatcher:
    """实现 `HotkeySink`：状态判定 + 主线程执行动作；进入点均可作 GLib.idle_add 回调（返回 False）。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        state: HotkeyPressState,
        *,
        on_error: Callable[[str | None], None],
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.on_error = on_error
        # 当前生效的语音组合（门户模式 = GNOME 实际绑定，否则 = 配置），看门狗复位按它补发修饰键抬起
        self.voice_hotkey = ctx.bindings.voice

    @property
    def voice_active(self) -> bool:
        return self.state.voice_active

    def interval_started(self, action: Action, s_ms: int) -> bool:
        self._run(self.state.interval_started(action, s_ms), f"按下区间开始 t={s_ms}")
        return False

    def interval_ended(self, action: Action, e_ms: int) -> bool:
        self._run(self.state.interval_ended(action, e_ms), f"按下区间结束 t={e_ms}")
        return False

    def activated(self, action: Action, t_ms: int) -> bool:
        self._run(self.state.activated(action, t_ms), f"放行 t={t_ms}")
        return False

    def reader_progress(self, p_ms: int) -> bool:
        self.state.reader_progress(p_ms)
        return False

    def escape_pressed(self) -> None:
        if self.ctx.actions.overlay_active():
            GLib.idle_add(self.ctx.actions.close_overlay)

    def source_failed(self, reason: str) -> bool:
        print(f"  快捷键: [ERROR] {reason}", flush=True)
        self.on_error(reason)
        return False

    def reset_voice(self) -> None:
        """看门狗强制结束录音后复位语音组合状态（不再调用 voice_stop），并补发当前生效组合的修饰键抬起。"""
        self.state.reset("voice")
        if self.ctx.janitor is not None:
            self.ctx.janitor.release_hotkey(self.voice_hotkey)

    def _run(self, edges: Iterable[Edge], cause: str) -> None:
        actions = self.ctx.actions
        for edge in edges:
            print(f"  快捷键: {edge.action} {edge.kind}（{cause}）", flush=True)
            if edge.kind == "voice_start":
                if not actions.voice_start():
                    self.state.start_failed()
            elif edge.kind == "voice_stop":
                actions.voice_stop()
            elif edge.action == "screenshot":
                actions.screenshot()
            elif edge.action == "ocr":
                actions.ocr()
            else:
                actions.repeat()
