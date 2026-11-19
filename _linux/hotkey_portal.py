"""xdg-desktop-portal GlobalShortcuts 后端（Wayland 主路径，支持四键与配置改键自动生效）。

根因修复：
1. GNOME `xdg-desktop-portal-gnome` 在 dconf 中按 `shortcut_id` 缓存历史键位，若 ID 恒为固定 `"screenshot"`，
   用户修改 `.config.json` 重启后 `BindShortcuts` 会直接忽略新的 `preferred_trigger` 并沿用旧键位！
   现将规范化触发器指纹编入快捷键 ID（如 `screenshot_ctrl_alt_x`），同时比对 `BindShortcuts` 返回的
   `actual_triggers` 与配置值，不一致时自动调用 `ConfigureShortcuts` 并发出明确重绑提示。
2. 监听 `org.freedesktop.portal.Desktop` D-Bus 总线名拥有者变更：portal 退出时通知回退观察器，
   portal 重启上线（新 owner 出现）时强制重新注册 `org.freedesktop.host.portal.Registry` 并重建会话。
"""
from __future__ import annotations

import os
import re
import time
import traceback

from _hotkey import HOTKEY_MODIFIERS, normalize_accel, normalize_hotkey, to_portal_trigger
from _linux.hotkey_actions import GLib, HotkeyActionMixin, HotkeyContext
from _linux.portal_dbus import (
    GLOBAL_SHORTCUTS_IFACE,
    PORTAL_NAME,
    PORTAL_PATH,
    dbus_module,
    portal_available,
    portal_interface_version,
    register_portal_host_app,
)

_KNOWN_ACTIONS = ("voice", "screenshot", "ocr", "repeat")
_DISCRETE_ACTIONS = frozenset({"screenshot", "ocr", "repeat"})
DISCRETE_DEBOUNCE_S = 0.35
DISCRETE_STALE_S = 1.5


def _canonicalize_modifier_order(normalized: str) -> str:
    """将修饰键按 `_hotkey.HOTKEY_MODIFIERS` 规范顺序重排，消除 `<Shift><Alt>x` 与 `alt+shift+x` 假阳性不匹配。"""
    parts = [p for p in normalized.split("+") if p]
    if not parts:
        return ""
    *raw_mods, main = parts
    mods = ["super" if m == "logo" else m for m in raw_mods]
    mod_set = set(mods)
    ordered = [m for m in HOTKEY_MODIFIERS if m in mod_set]
    extras = [m for m in mods if m not in HOTKEY_MODIFIERS]
    return "+".join([*ordered, *extras, main])


def portal_shortcut_id(action: str, hotkey: str) -> str:
    """生成带规范化触发器指纹的 Portal 快捷键 ID（如 `screenshot_ctrl_alt_x`）。"""
    norm = _canonicalize_modifier_order(normalize_hotkey(hotkey))
    slug = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")
    return f"{action}_{slug}" if slug else action


def action_from_shortcut_id(shortcut_id: str) -> str:
    """从带指纹 ID（如 `ocr_ctrl_alt_o`）或裸 ID（`ocr`）还原逻辑动作名。"""
    sid = str(shortcut_id).strip()
    if sid in _KNOWN_ACTIONS:
        return sid
    for action in _KNOWN_ACTIONS:
        if sid.startswith(f"{action}_") or sid.startswith(f"{action}:"):
            return action
    return sid


def normalize_portal_trigger(raw_trigger: str) -> str:
    """把 Portal 返回的 `CTRL+ALT+X` / `SHIFT+CTRL+S` / `LOGO+Z` / `<Shift><Alt>x` / `Press Ctrl+Alt+Z` 归一化为规范顺序串。"""
    text = (raw_trigger or "").strip()
    if not text:
        return ""
    if "<" in text and ">" in text:
        idx = text.find("<")
        return _canonicalize_modifier_order(normalize_accel(text[idx:]))
    cleaned = re.sub(r"^(?:(?:按|press)\s+|[^\x00-\x7f]+\s*)", "", text, flags=re.IGNORECASE).strip()
    tokens = [tok.strip().lower() for tok in cleaned.replace("-", "+").split("+") if tok.strip()]
    if not tokens:
        return ""
    mapped = ["super" if tok == "logo" else tok for tok in tokens]
    return _canonicalize_modifier_order(normalize_hotkey("+".join(mapped)))


def portal_trigger_matches(actual_trigger: str, configured_hotkey: str) -> bool:
    """判断 Portal 实际生效触发器是否与配置一致（忽略修饰键排列顺序与引导词前缀）。"""
    expected = _canonicalize_modifier_order(normalize_hotkey(configured_hotkey))
    actual = normalize_portal_trigger(actual_trigger)
    if not expected or not actual:
        return False
    return actual == expected


class PortalGlobalShortcutsGrabber(HotkeyActionMixin):
    """xdg-desktop-portal GlobalShortcuts 后端（Wayland 主路径）。"""

    _release_modifiers_on_stop = False
    DISCRETE_STALE_S = DISCRETE_STALE_S

    def __init__(
        self,
        ctx: HotkeyContext,
        on_bound=None,
        on_failed=None,
        *,
        dbus_mod=None,
        bus=None,
        version_probe=portal_interface_version,
        clock=None,
    ) -> None:
        self._init_hotkey_actions(ctx)
        self._on_bound_cb = on_bound
        self._on_failed_cb = on_failed
        self._clock = clock
        self._dbus = None
        self._bus = None
        self._iface = None
        self._session_handle: str | None = None
        self._signal_matches: list = []
        self._request_matches: list = []
        self._token_seq = 0
        self._active_shortcuts: set[str] = set()
        self._last_discrete_at: dict[str, float] = {}
        self._last_activated_at: dict[str, float] = {}
        self._closed = False
        self._failed = False
        self._bound = False
        self._configure_attempted = False
        self._portal_owner: str | None = None
        self.watching_owner = False
        self.actual_triggers: dict[str, str] = {}
        self.action_triggers: dict[str, str] = {}
        self.mismatched_triggers: dict[str, tuple[str, str]] = {}

        version = version_probe(GLOBAL_SHORTCUTS_IFACE)
        if version is None:
            raise RuntimeError("xdg-desktop-portal GlobalShortcuts 不可用")
        if version < 1:
            raise RuntimeError(f"GlobalShortcuts 版本过低: {version}")
        self._portal_version = int(version)

        try:
            self._dbus = dbus_mod if dbus_mod is not None else dbus_module()
            self._bus = bus if bus is not None else self._dbus.SessionBus()
            obj = self._bus.get_object(PORTAL_NAME, PORTAL_PATH)
            self._iface = self._dbus.Interface(obj, GLOBAL_SHORTCUTS_IFACE)
        except Exception as exc:
            raise RuntimeError(f"连接 portal GlobalShortcuts 失败: {exc}") from exc

        self._connect_signals()
        register_portal_host_app(self._bus, force=False, dbus_mod=self._dbus)
        self._owner_watch = None
        try:
            self._owner_watch = self._bus.watch_name_owner(
                PORTAL_NAME, self._on_portal_owner_changed
            )
            self.watching_owner = True
        except Exception:
            self.watching_owner = False

    def _on_portal_owner_changed(self, owner) -> None:
        if self._closed:
            return
        owner_str = str(owner or "").strip()
        if not owner_str:
            self._portal_owner = ""
            self._bound = False
            self._session_handle = None
            self._fail("xdg-desktop-portal 服务已退出")
            return
        prev = self._portal_owner
        self._portal_owner = owner_str
        # 初次 watch_name_owner 会立即回调一次当前 owner，此时 start() 流程自己负责建会话；
        # 仅当此前已记录过 owner（含曾变空退出或切换成新进程 owner）时执行重注册与会话重建。
        if prev is None:
            return
        if prev == owner_str and self._bound:
            return
        print(f"  快捷键: 检测到 {PORTAL_NAME} 总线名变更 ({prev!r} -> {owner_str!r})，重新注册宿主并恢复绑定", flush=True)
        self._failed = False
        self._bound = False
        self._session_handle = None
        self._configure_attempted = False
        self._active_shortcuts.clear()
        self._last_discrete_at.clear()
        self._last_activated_at.clear()
        try:
            self._clear_request_matches()
            self._clear_signal_matches()
            obj = self._bus.get_object(PORTAL_NAME, PORTAL_PATH)
            self._iface = self._dbus.Interface(obj, GLOBAL_SHORTCUTS_IFACE)
            self._connect_signals()
            register_portal_host_app(self._bus, force=True, dbus_mod=self._dbus)
            self._create_session()
        except Exception as exc:
            self._fail(f"portal 总线重连失败: {exc}")

    @classmethod
    def available(cls) -> bool:
        return portal_available(GLOBAL_SHORTCUTS_IFACE)

    def _clear_request_matches(self) -> None:
        for _req, match in self._request_matches:
            try:
                match.remove()
            except Exception:
                pass
        self._request_matches.clear()

    def _clear_signal_matches(self) -> None:
        for match in self._signal_matches:
            try:
                match.remove()
            except Exception:
                pass
        self._signal_matches.clear()

    def _connect_signals(self) -> None:
        for name, cb in (
            ("Activated", self._on_activated),
            ("Deactivated", self._on_deactivated),
            ("ShortcutsChanged", self._on_shortcuts_changed),
        ):
            try:
                self._signal_matches.append(self._iface.connect_to_signal(name, cb))
            except Exception as exc:
                raise RuntimeError(f"订阅 portal 信号 {name} 失败: {exc}") from exc

    def _new_token(self, prefix: str) -> str:
        self._token_seq += 1
        return f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 100000000}_{self._token_seq}"

    def _predict_request_path(self, token: str) -> str | None:
        """按 XDG Desktop Portal 规范推导请求对象路径，用于在 D-Bus 调用前预订阅 Response 信号。"""
        get_unique = getattr(self._bus, "get_unique_name", None)
        if not callable(get_unique):
            return None
        try:
            unique = str(get_unique() or "").strip()
        except Exception:
            return None
        if not unique:
            return None
        sender = unique.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _watch_response(self, handle, callback) -> tuple[object, object]:
        req = self._bus.get_object(PORTAL_NAME, handle)
        match = self._dbus.Interface(
            req, "org.freedesktop.portal.Request"
        ).connect_to_signal("Response", callback)
        entry = (req, match)
        self._request_matches.append(entry)
        return entry

    def _call_portal_request(self, token: str, call_fn, callback) -> None:
        """先按预测路径预订阅 `org.freedesktop.portal.Request.Response`，再发 D-Bus 调用，消除竞态丢信号。"""
        expected_path = self._predict_request_path(token)
        add_receiver = getattr(self._bus, "add_signal_receiver", None)
        matches: list[tuple[object, object]] = []
        handled = False

        def _cleanup_matches() -> None:
            for entry in list(matches):
                _req, match = entry
                try:
                    match.remove()
                except Exception:
                    pass
                if entry in self._request_matches:
                    self._request_matches.remove(entry)
            matches.clear()

        def _once(response, results) -> None:
            nonlocal handled
            if handled:
                return
            handled = True
            _cleanup_matches()
            callback(response, results)

        if expected_path and callable(add_receiver):
            match = add_receiver(
                _once,
                signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                bus_name=PORTAL_NAME,
                path=expected_path,
            )
            entry = (None, match)
            matches.append(entry)
            self._request_matches.append(entry)

        try:
            handle = str(call_fn() or "")
            if not handled and handle and handle != expected_path:
                if callable(add_receiver):
                    match = add_receiver(
                        _once,
                        signal_name="Response",
                        dbus_interface="org.freedesktop.portal.Request",
                        bus_name=PORTAL_NAME,
                        path=handle,
                    )
                    entry = (None, match)
                    self._request_matches.append(entry)
                else:
                    entry = self._watch_response(handle, _once)
                matches.append(entry)
        except Exception:
            _cleanup_matches()
            raise

    def start(self) -> None:
        if self._closed:
            return
        self._create_session()

    def _create_session(self) -> None:
        token = self._new_token("ovi_session")
        options = self._dbus.Dictionary(
            {
                "handle_token": self._dbus.String(token),
                "session_handle_token": self._dbus.String(token),
            },
            signature="sv",
        )
        try:
            self._call_portal_request(
                token,
                lambda: self._iface.CreateSession(options),
                self._on_session_response,
            )
        except Exception as exc:
            self._fail(f"CreateSession 调用失败: {exc}")

    def _on_session_response(self, response, results) -> None:
        if self._closed:
            return
        if int(response) != 0:
            self._fail(f"CreateSession 被拒绝 (response={int(response)})")
            return
        session = results.get("session_handle") if results else None
        if not session:
            self._fail("CreateSession 未返回 session_handle")
            return
        self._session_handle = str(session)
        self._bind_shortcuts()

    def _shortcut_specs(self) -> tuple[tuple[str, str, str, str], ...]:
        """返回 `(action, fingerprinted_shortcut_id, description, configured_hotkey)` 四元组。"""
        entries = [
            ("voice", "语音输入", self.ctx.bindings.voice),
            ("screenshot", "截图翻译", self.ctx.bindings.screenshot),
            ("ocr", "纯OCR识别", self.ctx.bindings.ocr),
            ("repeat", "重复输入", self.ctx.bindings.repeat),
        ]
        return tuple(
            (action, portal_shortcut_id(action, hotkey), desc, hotkey)
            for action, desc, hotkey in entries
            if hotkey and hotkey.strip()
        )

    def _bind_shortcuts(self) -> None:
        shortcuts = []
        for _action, sid, desc, hotkey in self._shortcut_specs():
            props = self._dbus.Dictionary(
                {
                    "description": self._dbus.String(desc),
                    "preferred_trigger": self._dbus.String(to_portal_trigger(hotkey)),
                },
                signature="sv",
            )
            shortcuts.append((self._dbus.String(sid), props))
        token = self._new_token("ovi_bind")
        options = self._dbus.Dictionary(
            {"handle_token": self._dbus.String(token)}, signature="sv"
        )
        try:
            self._call_portal_request(
                token,
                lambda: self._iface.BindShortcuts(
                    self._dbus.ObjectPath(self._session_handle),
                    self._dbus.Array(shortcuts, signature="(sa{sv})"),
                    "",
                    options,
                ),
                self._on_bind_response,
            )
        except Exception as exc:
            self._fail(f"BindShortcuts 调用失败: {exc}")

    def _parse_shortcuts(self, raw) -> dict[str, str]:
        out: dict[str, str] = {}
        if not raw:
            return out
        try:
            for sid, props in raw:
                trigger = (
                    props.get("trigger")
                    or props.get("preferred_trigger")
                    or props.get("trigger_description")
                    or ""
                )
                out[str(sid)] = str(trigger)
        except Exception:
            pass
        return out

    def _sync_action_triggers_and_verify(self, *, allow_configure: bool = True) -> None:
        self.action_triggers = {
            action_from_shortcut_id(sid): trig for sid, trig in self.actual_triggers.items()
        }
        mismatches: dict[str, tuple[str, str]] = {}
        for action, sid, _desc, configured in self._shortcut_specs():
            actual = self.actual_triggers.get(sid) or self.action_triggers.get(action, "")
            if actual and not portal_trigger_matches(actual, configured):
                mismatches[action] = (configured, actual)
                print(
                    f"  快捷键: [WARN] Portal 实际绑定 {action}={actual!r} 与配置 {configured!r} 不一致！"
                    " 将尝试调用 ConfigureShortcuts 更新，或请在系统设置 -> 键盘 -> 应用快捷键中重绑。",
                    flush=True,
                )
        self.mismatched_triggers = mismatches
        if allow_configure and mismatches and not self._configure_attempted:
            self._configure_attempted = True
            self._configure_shortcuts()

    def _configure_shortcuts(self) -> bool:
        """当 Portal 返回的旧触发器与配置不一致时，尝试调用 GlobalShortcuts v2 ConfigureShortcuts。"""
        if not self._session_handle or self._iface is None:
            return False
        cfg_fn = getattr(self._iface, "ConfigureShortcuts", None)
        if not callable(cfg_fn):
            return False
        try:
            token = self._new_token("ovi_cfg")
            options = self._dbus.Dictionary({"handle_token": self._dbus.String(token)}, signature="sv")
            cfg_fn(self._dbus.ObjectPath(self._session_handle), "", options)
            return True
        except Exception as exc:
            print(
                f"  快捷键: ConfigureShortcuts 不可用或被忽略 ({exc})；若键位未更新请在系统设置重绑",
                flush=True,
            )
            return False

    def _on_bind_response(self, response, results) -> None:
        if self._closed:
            return
        if int(response) != 0:
            self._fail(f"BindShortcuts 被拒绝 (response={int(response)})")
            return
        self._bound = True
        self._failed = False
        self.actual_triggers = self._parse_shortcuts(
            results.get("shortcuts") if results else None
        )
        print("  快捷键: portal GlobalShortcuts 已绑定", flush=True)
        for sid, trigger in self.actual_triggers.items():
            print(f"    {sid}: {trigger}", flush=True)
        self._sync_action_triggers_and_verify()
        if self._on_bound_cb:
            try:
                self._on_bound_cb()
            except Exception:
                traceback.print_exc()

    def _on_shortcuts_changed(self, session_handle, shortcuts) -> None:
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        self.actual_triggers = self._parse_shortcuts(shortcuts)
        self._sync_action_triggers_and_verify(allow_configure=False)
        print(f"  快捷键: portal 键位已更新 {self.actual_triggers}", flush=True)

    def _on_activated(self, session_handle, shortcut_id, timestamp, options) -> None:
        del timestamp, options
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        action = action_from_shortcut_id(str(shortcut_id))
        if action in _DISCRETE_ACTIONS:
            now = self._clock() if self._clock is not None else time.monotonic()
            # 根治：GNOME Portal 在长按离散快捷键时会以 ~30Hz 连发 Activated，
            # 必须按活跃态 + 350ms 防抖拦截重复触发；同时若前一次 Deactivated 信号丢失，
            # 超过 DISCRETE_STALE_S 自动清除过期活跃态并放行本次新触发。
            if action in self._active_shortcuts:
                if (now - self._last_activated_at.get(action, 0.0)) < self.DISCRETE_STALE_S:
                    self._last_activated_at[action] = now
                    return
                self._active_shortcuts.discard(action)
                self._last_activated_at.pop(action, None)
            last = self._last_discrete_at.get(action, -1e9)
            if (now - last) < DISCRETE_DEBOUNCE_S:
                return
            self._active_shortcuts.add(action)
            self._last_discrete_at[action] = now
            self._last_activated_at[action] = now
        GLib.idle_add(self._idle_action, action)

    def _idle_action(self, sid: str) -> bool:
        if self._closed:
            return False
        action = action_from_shortcut_id(sid)
        if action == "voice":
            self._on_voice_press()
        elif action == "screenshot":
            self._on_screenshot()
        elif action == "ocr":
            self._on_ocr()
        elif action == "repeat":
            self._on_repeat()
        return False

    def _on_deactivated(self, session_handle, shortcut_id, timestamp, options) -> None:
        del timestamp, options
        if self._session_handle and str(session_handle) != self._session_handle:
            return
        action = action_from_shortcut_id(str(shortcut_id))
        self._active_shortcuts.discard(action)
        self._last_activated_at.pop(action, None)
        if action == "voice":
            GLib.idle_add(self._idle_stop)

    def _idle_stop(self) -> bool:
        if not self._closed:
            self._do_release()
        return False

    def _fail(self, message: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        print(f"  快捷键: portal GlobalShortcuts 失败: {message}", flush=True)
        if self._on_failed_cb:
            try:
                self._on_failed_cb()
            except Exception:
                traceback.print_exc()

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.watching_owner = False
        self._active_shortcuts.clear()
        self._last_discrete_at.clear()
        self._last_activated_at.clear()
        self._cancel_pending_release()
        self._clear_request_matches()
        self._clear_signal_matches()
        if self._owner_watch is not None:
            try:
                self._bus.unwatch_name_owner(self._owner_watch)
            except Exception:
                pass
            self._owner_watch = None
        if self._session_handle:
            try:
                sess = self._bus.get_object(PORTAL_NAME, self._session_handle)
                self._dbus.Interface(sess, "org.freedesktop.portal.Session").Close()
            except Exception:
                pass
        self._session_handle = None
