"""按会话装配快捷键后端，并把它们接到唯一的按住状态与分发器（业务装配）。

Wayland：evdev 必需（按下区间的唯一来源）；门户 GlobalShortcuts 负责放行，组合定义取 GNOME 实际绑定下发给 evdev；
门户不可用或失败时 evdev 回退为自行判定开始（按配置绑定）。XInput 只做 XTEST 卡键清理，不进状态机。
X11：XGrabKey 独占；失败回退 evdev + XInput（只收 XTEST 来源）。
组件失败（evdev 打不开、实际绑定解析不了、事件源意外退出、全部后端不可用）只经 on_error 上报，不抛异常，
守护进程其余功能照常；之后恢复正常时 on_error(None)。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from _hotkey import parse_hotkey_evdev
from _linux.hotkey_actions import HotkeyContext
from _linux.hotkey_dispatch import HotkeyDispatcher
from _linux.hotkey_evdev import EvdevHotkeyGrabber
from _linux.hotkey_portal import PortalGlobalShortcutsGrabber, normalize_portal_trigger
from _linux.hotkey_state import ACTIONS, Action, HotkeyPressState
from _linux.hotkey_x11 import HotkeyGrabber
from _linux.hotkey_xinput import XInputHotkeyGrabber


class HotkeyManager:
    """按会话选择后端；唯一的 HotkeyPressState + HotkeyDispatcher 作为全部后端的 sink。"""

    def __init__(
        self,
        ctx: HotkeyContext,
        *,
        on_error: Callable[[str | None], None],
        evdev_factory=EvdevHotkeyGrabber,
        portal_factory=PortalGlobalShortcutsGrabber,
        portal_available: Callable[[], bool] = PortalGlobalShortcutsGrabber.available,
        x11_factory=HotkeyGrabber,
        xinput_factory=XInputHotkeyGrabber,
    ) -> None:
        self.ctx = ctx
        self.on_error = on_error
        self.dispatcher = HotkeyDispatcher(ctx, HotkeyPressState(), on_error=on_error)
        self._evdev_factory = evdev_factory
        self._portal_factory = portal_factory
        self._portal_available = portal_available
        self._x11_factory = x11_factory
        self._xinput_factory = xinput_factory
        self.backend_name = "none"
        self._evdev: EvdevHotkeyGrabber | None = None
        self._xinput: XInputHotkeyGrabber | None = None
        self._portal: PortalGlobalShortcutsGrabber | None = None
        self._x11: HotkeyGrabber | None = None
        self._closed = False
        if self.ctx.janitor is not None and getattr(self.ctx.janitor, "evdev_any_modifier_down", None) is None:
            self.ctx.janitor.evdev_any_modifier_down = self.evdev_any_modifier_down

    def evdev_any_modifier_down(self) -> bool:
        """供 ModifierJanitor 在 Wayland 下查询物理修饰键是否仍处于按下态。"""
        return self._evdev is not None and self._evdev.any_modifier_down()

    def reset_voice(self) -> None:
        """看门狗超时后复位语音状态并补发当前生效组合的修饰键抬起（不重复调用 voice_stop）。"""
        self.dispatcher.reset_voice()

    def start(self) -> None:
        if self.ctx.session_type == "wayland":
            self._start_wayland()
        else:
            self._start_x11()

    # ---- Wayland ----------------------------------------------------------
    def _start_wayland(self) -> None:
        try:
            self._evdev = self._evdev_factory(self.ctx, self.dispatcher)
        except Exception as exc:
            self._fail(f"Wayland 下快捷键需要读取键盘设备（evdev），当前不可用：{exc}")
            return
        self._evdev.start()
        print("  快捷键: evdev 已启动（按下区间来源）", flush=True)
        self._start_xinput_cleaner()
        if not self._portal_available():
            self._activate_evdev_fallback("xdg-desktop-portal GlobalShortcuts 不可用")
            return
        try:
            self._portal = self._portal_factory(
                self.ctx,
                self.dispatcher,
                on_bound=self._on_portal_bound,
                on_failed=self._on_portal_failed,
                on_triggers=self._apply_actual_triggers,
            )
        except Exception as exc:
            self._activate_evdev_fallback(f"portal 初始化失败：{exc}")
            return
        print("  快捷键: 启用 portal GlobalShortcuts — compositor 独占", flush=True)
        self._portal.start()

    def _start_xinput_cleaner(self) -> None:
        try:
            self._xinput = self._xinput_factory(self.ctx, self.dispatcher)
        except Exception as exc:
            print(f"  快捷键: XInput 卡键清理不可用 ({exc})", flush=True)
            return
        self._xinput.actions_enabled = False  # Wayland 下不进状态机，只做 XTEST 卡键清理
        self._xinput.only_xtest_sources = True
        self._xinput.start()

    def _on_portal_bound(self) -> None:
        self.backend_name = "portal"
        if self._evdev is not None:
            self._evdev.activations_enabled = False
        print("  快捷键: portal 已接管；evdev 只报按下区间", flush=True)

    def _on_portal_failed(self) -> None:
        if self._portal is not None and not getattr(self._portal, "watching_owner", False):
            self._portal.cleanup()
        self._activate_evdev_fallback("portal GlobalShortcuts 失败")

    def _activate_evdev_fallback(self, reason: str) -> None:
        if self._evdev is None:
            return
        self._evdev.activations_enabled = True
        self._install_combos({action: self._configured(action) for action in ACTIONS})
        self.dispatcher.voice_hotkey = self.ctx.bindings.voice
        self.backend_name = "evdev"
        print(f"  快捷键: {reason}，启用 evdev 回退（按键未独占，可能仍会传给应用）", flush=True)

    def _apply_actual_triggers(self, action_triggers: Mapping[str, str]) -> None:
        """GNOME 实际绑定是组合定义的唯一来源：规范化 + 严格解析后下发给 evdev。"""
        if self._evdev is None:
            return
        canonical = {
            action: normalize_portal_trigger(action_triggers.get(action, ""))
            for action in ACTIONS
            if self._configured(action)
        }
        self._install_combos(canonical)
        self.dispatcher.voice_hotkey = canonical.get("voice") or self.ctx.bindings.voice

    def _install_combos(self, hotkeys: Mapping[Action, str]) -> None:
        """解析并下发给 evdev：能解析的照常生效；有解析不了的动作就按组件失败上报，全部可用时报恢复。"""
        combos: dict[Action, dict] = {}
        failed: list[str] = []
        for action, hotkey in hotkeys.items():
            if not hotkey:
                failed.append(f"{action}=（GNOME 未给出绑定）")
                continue
            try:
                combos[action] = parse_hotkey_evdev(hotkey)
            except ValueError as exc:
                failed.append(f"{action}={hotkey!r}（{exc}）")
        self._evdev.set_combos(combos)
        if failed:
            self._fail("快捷键实际绑定无法解析为键码：" + "；".join(failed))
        else:
            self.on_error(None)

    def _configured(self, action: Action) -> str:
        return (getattr(self.ctx.bindings, action) or "").strip()

    # ---- X11 --------------------------------------------------------------
    def _start_x11(self) -> None:
        try:
            self._x11 = self._x11_factory(self.ctx, self.dispatcher)
        except Exception as exc:
            print(f"  快捷键: XGrabKey 不可用 ({exc})，尝试回退", flush=True)
            self._start_x11_fallback()
            return
        self.backend_name = "XGrabKey"
        print("  快捷键: 启用 XGrabKey — X11 独占", flush=True)
        self._x11.start()

    def _start_x11_fallback(self) -> None:
        names: list[str] = []
        try:
            self._evdev = self._evdev_factory(self.ctx, self.dispatcher)
        except Exception as exc:
            print(f"  快捷键: evdev 不可用 ({exc})", flush=True)
        else:
            self._evdev.activations_enabled = True
            self._install_combos({action: self._configured(action) for action in ACTIONS})
            self._evdev.start()
            names.append("evdev")
        try:
            self._xinput = self._xinput_factory(self.ctx, self.dispatcher)
        except Exception as exc:
            print(f"  快捷键: XInput 不可用 ({exc})", flush=True)
        else:
            self._xinput.actions_enabled = True
            self._xinput.only_xtest_sources = self._evdev is not None
            self._xinput.start()
            names.append("xinput")
        if not names:
            self._fail("所有快捷键抓取方式均不可用（XGrabKey、evdev、XInput）")
            return
        self.backend_name = "+".join(names)
        print("  快捷键: 启用观察器回退（按键未独占，可能仍会传给应用）", flush=True)

    # ---- 共用 -------------------------------------------------------------
    def _fail(self, reason: str) -> None:
        print(f"  快捷键: [ERROR] {reason}", flush=True)
        self.on_error(reason)

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        for backend in (self._portal, self._evdev, self._xinput, self._x11):
            if backend is None:
                continue
            try:
                backend.cleanup()
            except Exception as exc:  # 退出清理：逐个后端记录失败原因，不影响其余后端与守护进程退出
                print(f"  快捷键: 清理 {type(backend).__name__} 失败：{type(exc).__name__}: {exc}", flush=True)
