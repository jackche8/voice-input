"""IBus 输入法直接提交（Tier 1）。通过 IBus 私有总线探测 CurrentInputContext 真实可用性。

根因修复：
IBus 并不在标准 `--session` D-Bus 上暴露 `/org/freedesktop/IBus` 节点；旧实现用
`gdbus introspect --session --dest org.freedesktop.IBus --object-path /org/freedesktop/IBus`
恒返回空 `node {}`，导致 GNOME Wayland 会话永久误判 Tier-1 IBus 不可用而退化到剪贴板粘贴。
正确路径是通过 `ibus address`（及 `~/.config/ibus/bus/` 私有总线记录）获取 IBus 私有 socket 地址，
以 `Gio.DBusConnection.new_for_address_sync(..., AUTHENTICATION_CLIENT, None, None)` 直连探测
`org.freedesktop.IBus.CurrentInputContext`。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path


def _extract_unix_socket_path(address: str) -> Path | None:
    for part in address.split(";"):
        if not part.startswith("unix:"):
            continue
        for kv in part[5:].split(","):
            if kv.startswith("path="):
                raw = kv[5:].strip()
                if raw:
                    return Path(raw)
    return None


def _address_socket_usable(address: str) -> bool:
    sock = _extract_unix_socket_path(address)
    if sock is None:
        return bool(address.strip())
    return sock.exists()


def _sync_ibus_address_env(
    address: str | None,
    env: MutableMapping[str, str] | None = None,
) -> bool:
    """当环境中的 `IBUS_ADDRESS` 缺失或指向已失效旧 socket 时，用新探测到的有效地址覆盖更新。"""
    if not address or not address.strip():
        return False
    target = os.environ if env is None else env
    current = (target.get("IBUS_ADDRESS") or "").strip()
    if not current or not _address_socket_usable(current):
        target["IBUS_ADDRESS"] = address
        return True
    return False


def resolve_ibus_address(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    bus_dir: Path | None = None,
) -> str | None:
    """获取当前会话的 IBus 私有 D-Bus 地址。"""
    env = os.environ if environ is None else environ
    explicit = (env.get("IBUS_ADDRESS") or "").strip()
    if explicit and _address_socket_usable(explicit):
        return explicit

    ibus_bin = which("ibus")
    if ibus_bin:
        try:
            proc = run(
                [ibus_bin, "address"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=dict(env),
            )
            if proc.returncode == 0:
                addr = (proc.stdout or "").strip()
                if addr and _address_socket_usable(addr):
                    return addr
        except Exception:
            pass

    # GNOME Wayland 下守护进程以 GDK_BACKEND=x11 运行时，`ibus address` 可能误读空的 `-unix-0`
    # 而忽略活跃的 `-unix-wayland-0`；回退扫描 ~/.config/ibus/bus。
    if bus_dir is None:
        cfg_home = Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        bus_dir = cfg_home / "ibus" / "bus"
    if not bus_dir.is_dir():
        return None

    prefer_wayland = env.get("XDG_SESSION_TYPE") == "wayland" or bool(env.get("WAYLAND_DISPLAY"))
    try:
        candidates = sorted(
            (p for p in bus_dir.iterdir() if p.is_file()),
            key=lambda p: (
                1 if (prefer_wayland and "wayland" in p.name.lower()) else 0,
                p.stat().st_mtime,
            ),
            reverse=True,
        )
    except OSError:
        return None

    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("IBUS_ADDRESS="):
                    addr = line.partition("=")[2].strip()
                    if addr and _address_socket_usable(addr):
                        return addr
        except OSError:
            continue
    return None


def _default_private_bus_call(address: str) -> str:
    import gi

    from gi.repository import Gio, GLib

    conn = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT,
        None,
        None,
    )
    try:
        variant = conn.call_sync(
            "org.freedesktop.IBus",
            "/org/freedesktop/IBus",
            "org.freedesktop.IBus",
            "CurrentInputContext",
            None,
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        unpacked = variant.unpack()
        return str(unpacked[0]) if unpacked else ""
    finally:
        try:
            conn.close_sync(None)
        except Exception:
            pass


def probe_ibus_private_bus(
    address: str,
    *,
    connection_factory: Callable[[str], object] | None = None,
) -> tuple[bool, str]:
    """直连 IBus 私有总线地址并调用 CurrentInputContext 验证直提交能力。"""
    if not address or not address.strip():
        return False, "IBus 私有总线地址为空"
    try:
        if connection_factory is None:
            ctx_path = _default_private_bus_call(address)
        else:
            conn = connection_factory(address)
            try:
                if hasattr(conn, "call_current_input_context"):
                    ctx_path = str(conn.call_current_input_context() or "")
                else:
                    from gi.repository import Gio, GLib

                    variant = conn.call_sync(
                        "org.freedesktop.IBus",
                        "/org/freedesktop/IBus",
                        "org.freedesktop.IBus",
                        "CurrentInputContext",
                        None,
                        GLib.VariantType.new("(o)"),
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                    unpacked = variant.unpack()
                    ctx_path = str(unpacked[0]) if unpacked else ""
            finally:
                for close_name in ("close_sync", "close"):
                    close_fn = getattr(conn, close_name, None)
                    if callable(close_fn):
                        try:
                            close_fn(None) if close_name == "close_sync" else close_fn()
                        except TypeError:
                            close_fn()
                        except Exception:
                            pass
                        break
    except Exception as exc:
        detail = (str(exc).splitlines() or [type(exc).__name__])[0]
        lowered = detail.lower()
        # 启动瞬间可能没有处于编辑态的输入框，IBus 返回 "No input context" / "Bus has no focus"，
        # 说明私有总线已连通且 CurrentInputContext 接口本身受支持。
        if "no input context" in lowered or "bus has no focus" in lowered:
            return True, f"IBus 私有总线支持 CurrentInputContext（当前暂无焦点输入框: {detail}）"
        return False, f"IBus 私有总线不支持 CurrentInputContext ({detail})"

    if ctx_path:
        return True, f"IBus 私有总线已就绪，检测到输入上下文 {ctx_path}"
    return True, "IBus 私有总线支持 CurrentInputContext，将按需直接提交"


def probe_ibus_commit_support(
    bus=None,
    *,
    address: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    bus_dir: Path | None = None,
    connection_factory: Callable[[str], object] | None = None,
) -> tuple[bool, str]:
    """探测当前会话是否支持 IBus CurrentInputContext 直提交流程。"""
    if bus is not None and not bus.is_connected():
        return False, "IBus 守护进程未连接"

    resolved = address or resolve_ibus_address(
        environ=environ,
        which=which,
        run=run,
        bus_dir=bus_dir,
    )
    if resolved:
        return probe_ibus_private_bus(resolved, connection_factory=connection_factory)

    if bus is None:
        return False, "无法获取 IBus 私有总线地址"

    try:
        ctx_path = bus.current_input_context()
    except Exception as exc:
        detail = (str(exc).splitlines() or [type(exc).__name__])[0]
        lowered = detail.lower()
        if "no input context" in lowered or "bus has no focus" in lowered:
            return True, f"当前会话支持 current_input_context（暂无焦点输入框: {detail}）"
        return False, f"当前会话不支持 current_input_context ({detail})"
    if ctx_path:
        return True, f"已检测到输入上下文 {ctx_path}"
    return True, "当前会话支持 current_input_context，将按需尝试直接提交"


class IBusCommitter:
    def __init__(self, bus=None, *, available: bool = False, reason: str = "未初始化") -> None:
        self.bus = bus
        self.available = available
        self.reason = reason

    @classmethod
    def connect(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        bus_dir: Path | None = None,
        connection_factory: Callable[[str], object] | None = None,
        bus_factory: Callable[[str | None], object] | None = None,
    ) -> "IBusCommitter":
        try:
            addr = resolve_ibus_address(environ=environ, which=which, run=run, bus_dir=bus_dir)
            if addr and environ is None:
                _sync_ibus_address_env(addr, os.environ)
            if bus_factory is not None:
                bus = bus_factory(addr)
            else:
                import gi

                gi.require_version("IBus", "1.0")
                from gi.repository import IBus as IBusLib

                if addr:
                    _sync_ibus_address_env(addr, os.environ)
                bus = IBusLib.Bus()
            if bus is None or not bus.is_connected():
                return cls(None, available=False, reason="IBus 守护进程未运行")
            available, note = probe_ibus_commit_support(
                bus,
                address=addr,
                environ=environ,
                which=which,
                run=run,
                bus_dir=bus_dir,
                connection_factory=connection_factory,
            )
            return cls(bus if available else None, available=available, reason=note)
        except Exception as exc:
            return cls(None, available=False, reason=str(exc))

    def commit(self, text: str) -> bool:
        """通过 IBus 输入法框架直接提交文字到焦点窗口的输入上下文。"""
        if self.bus is None or not self.available:
            return False
        try:
            import gi

            gi.require_version("IBus", "1.0")
            from gi.repository import IBus as IBusLib

            if not self.bus.is_connected():
                return False
            ctx_path = self.bus.current_input_context()
            if not ctx_path:
                return False
            ctx = IBusLib.InputContext.get_input_context(ctx_path, self.bus.get_connection())
            if ctx is None:
                return False
            ctx.commit_text(IBusLib.Text.new_from_string(text))
            return True
        except Exception as exc:
            detail = (str(exc).splitlines() or [type(exc).__name__])[0]
            lowered = detail.lower()
            if "no input context" in lowered or "bus has no focus" in lowered:
                return False
            self.bus = None
            self.available = False
            self.reason = detail
            print(f"  IBus: 直接提交不可用，回退到剪贴板模式 ({detail})", flush=True)
            return False
