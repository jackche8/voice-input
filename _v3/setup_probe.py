"""设置页的真实状态探测（逻辑层）：只读，不启动、不下载、不改系统。

每个探测返回结构化事实与可见错误文本；不把探测失败当成就绪。
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from _contracts import SetupCapability
from _ocr import default_ocr_languages

if TYPE_CHECKING:  # 只用于类型标注
    from _linux.conflicts import ConflictScan


@dataclass(frozen=True)
class OllamaProbe:
    installed: bool
    running: bool
    version: str | None
    has_model: bool
    model_size: int | None
    error: str | None


def probe_ollama(*, binary: Path, client: httpx.Client, model: str) -> OllamaProbe:
    """binary 是产品使用的用户级官方 Ollama 可执行文件；client 指向它的回环地址。"""
    installed = binary.is_file() and os.access(binary, os.X_OK)
    try:
        version_resp = client.get("/api/version")
        version_resp.raise_for_status()
        version = version_resp.json().get("version")
        tags_resp = client.get("/api/tags")
        tags_resp.raise_for_status()
        models = tags_resp.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return OllamaProbe(installed, False, None, False, None, f"Ollama 未响应: {exc}")
    match = next((m for m in models if model in {m.get("name"), m.get("model")}), None)
    size = match.get("size") if isinstance(match, dict) else None
    return OllamaProbe(installed, True, version, match is not None, size, None)


@dataclass(frozen=True)
class AsrWeightsProbe:
    complete: bool
    bytes_on_disk: int
    missing: tuple[str, ...]


def _dir_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def probe_asr_weights(model_dir: Path) -> AsrWeightsProbe:
    """权重完整 = config.json 存在，且 safetensors 索引引用的全部分片存在，且无未完成的下载片段。"""
    if not model_dir.is_dir():
        return AsrWeightsProbe(False, 0, ("模型目录不存在",))
    missing: list[str] = []
    if not (model_dir / "config.json").is_file():
        missing.append("config.json")
    index = model_dir / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        except (OSError, ValueError, KeyError) as exc:
            missing.append(f"索引无法解析: {exc}")
            shards = set()
        missing.extend(sorted(name for name in shards if not (model_dir / name).is_file()))
    elif not any(model_dir.glob("*.safetensors")):
        missing.append("*.safetensors")
    if any(model_dir.rglob("*.incomplete")):
        missing.append("存在未完成的下载片段")
    return AsrWeightsProbe(not missing, _dir_bytes(model_dir), tuple(missing))


@dataclass(frozen=True)
class SidecarProbe:
    running: bool
    loaded: bool
    error: str | None


def probe_asr_sidecar(client: httpx.Client) -> SidecarProbe:
    try:
        resp = client.get("/health")
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return SidecarProbe(False, False, f"识别服务未响应: {exc}")
    return SidecarProbe(bool(payload.get("ok")), bool(payload.get("loaded")), None)


PortalVersionFn = Callable[[str], tuple[int | None, str | None]]
PORTAL_SCREENSHOT = "org.freedesktop.portal.Screenshot"
PORTAL_SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"


def dbus_portal_version(interface: str) -> tuple[int | None, str | None]:
    """xdg-desktop-portal 接口版本（沿用工作区 portal 检测语义）。失败返回原因，不吞成 False。"""
    try:
        import dbus

        bus = dbus.SessionBus()
        obj = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return int(props.Get(interface, "version")), None
    except Exception as exc:  # dbus 的异常类型随绑定而异，原因原样返回给设置页
        return None, f"{type(exc).__name__}: {exc}"


def session_type(environ: Mapping[str, str]) -> str:
    value = (environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    return value or "unknown"


def _tool(which: Callable[[str], str | None], name: str, label: str, *, required: bool) -> SetupCapability:
    path = which(name)
    return SetupCapability(
        id=name,
        label=label,
        available=path is not None,
        required=required,
        detail=path or f"未找到 {name}",
    )


def _portal(portal_version: PortalVersionFn, interface: str, cap_id: str, label: str, *, required: bool) -> SetupCapability:
    version, error = portal_version(interface)
    return SetupCapability(
        id=cap_id,
        label=label,
        available=version is not None,
        required=required,
        detail=f"version {version}" if version is not None else error,
    )


def _evdev_keyboards(keyboard_devices: Callable[[], list[str]]) -> SetupCapability:
    """Wayland 下快捷键的按下与松开只能从 evdev 读到：读不到键盘类设备时快捷键组件不可用。"""
    try:
        paths = keyboard_devices()
        error = None
    except Exception as exc:  # python-evdev 缺失等探测失败，原因直接展示
        paths, error = [], f"{type(exc).__name__}: {exc}"
    if error is None and not paths:
        error = "当前用户读不到任何键盘设备（/dev/input/event*），通常需加入 input 组并重新登录"
    return SetupCapability(
        id="evdev_keyboards",
        label="物理按键读取（evdev）",
        available=bool(paths),
        required=True,
        detail=error or "、".join(paths),
    )


def probe_capabilities(
    *,
    session: str,
    which: Callable[[str], str | None],
    portal_version: PortalVersionFn,
    ocr_languages: Callable[[], list[str]],
    keyboard_devices: Callable[[], list[str]],
) -> list[SetupCapability]:
    caps = [
        _tool(which, "xclip", "剪贴板 xclip", required=True),
        _tool(which, "notify-send", "桌面通知 notify-send", required=False),
    ]
    wayland = session == "wayland"
    x11 = session == "x11"
    if session not in {"wayland", "x11"}:
        caps.append(
            SetupCapability(
                id="session_type",
                label="桌面会话类型",
                available=False,
                required=True,
                detail=f"无法判断会话类型（XDG_SESSION_TYPE={session}）",
            )
        )
    if not x11:
        caps.append(_portal(portal_version, PORTAL_SCREENSHOT, "portal_screenshot", "Wayland 截图 portal", required=wayland))
        caps.append(_portal(portal_version, PORTAL_SHORTCUTS, "portal_shortcuts", "Wayland 全局快捷键 portal", required=wayland))
    if wayland:
        caps.append(_evdev_keyboards(keyboard_devices))
    if not wayland:
        caps.append(_tool(which, "maim", "X11 截图 maim", required=x11))
    try:
        langs = set(ocr_languages())
        lang_error = None
    except Exception as exc:  # pytesseract/tesseract 缺失时原因直接展示
        langs, lang_error = set(), f"{type(exc).__name__}: {exc}"
    for code, label in default_ocr_languages():
        caps.append(
            SetupCapability(
                id=f"ocr_{code}",
                label=label,
                available=code in langs,
                required=True,
                detail=lang_error or (None if code in langs else f"tesseract 缺少 {code} 语言包"),
            )
        )
    caps.append(
        SetupCapability(
            id="ocr_jpn",
            label="OCR 日文（默认不安装）",
            available="jpn" in langs,
            required=False,
            detail=None if "jpn" in langs else "未安装 jpn 语言包；默认不安装",
        )
    )
    return caps


HOTKEY_CONFLICTS_ID = "hotkey_conflicts"


def hotkey_conflicts_capability(scan: Callable[[], "ConflictScan"]) -> SetupCapability:
    """快捷键冲突能力项：唯一扫描器 `_linux.conflicts.scan_hotkey_conflicts` 的结果如实呈现。
    unread 非空时不论有无冲突都不可用（不能宣称无冲突）；扫描本身异常 → 不可用并写原因。只读，不改任何键位/授权。"""
    label = "快捷键冲突检查"
    try:
        result = scan()
    except Exception as exc:  # 配置读取/扫描异常都必须可见，不当无冲突
        return SetupCapability(id=HOTKEY_CONFLICTS_ID, label=label, available=False, required=False,
                               detail=f"无法检查快捷键冲突：{type(exc).__name__}: {exc}")
    parts: list[str] = []
    if result.conflicts:
        parts.append("与已登记的快捷键冲突：" + "；".join(result.conflicts))
    if result.unread:
        parts.append("未能读取（不能据此判定无冲突）：" + "；".join(result.unread))
    if parts:
        return SetupCapability(id=HOTKEY_CONFLICTS_ID, label=label, available=False, required=False,
                               detail="。".join(parts))
    return SetupCapability(
        id=HOTKEY_CONFLICTS_ID, label=label, available=True, required=False,
        detail="已读取的 GNOME 键位与其它应用全局快捷键登记中未发现冲突（第三方 evdev 监听与应用内快捷键无法检测）",
    )
