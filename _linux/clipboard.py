"""系统剪贴板读写与 MIME 类型无损快照（xclip 优先）。失败返回明确原因，由调用方决定可见性。

daemon 以 GDK_BACKEND=x11 (XWayland) 运行：
- xclip 写入 X11 CLIPBOARD → Mutter 自动桥接到 Wayland 剪贴板
- wl-copy 在 GNOME 上需要创建临时窗口抢焦点获取 serial，
  导致开始菜单闪烁且可能因无法获取焦点而静默失败

图片保护根因修复：
当用户剪贴板里只有截图（如 `image/png`）而无纯文本时，盲调 `xclip -selection clipboard -o` 会因找不到
`STRING` target 失败返回空串，随后粘贴完把空串写回剪贴板，导致用户刚复制的截图永久丢失。
现先探测 `TARGETS`（X11 `xclip -t TARGETS -o` / Wayland `wl-paste --list-types`），若为纯图片则完整快照
原始字节与 MIME 类型，粘贴后精确写回原图片剪贴板。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

_TEXT_TARGETS = frozenset({"utf8_string", "string", "text", "compound_text"})
_META_TARGETS = frozenset({"targets", "timestamp", "multiple", "save_targets", "atom"})
_UNSUPPORTED_TARGETS = frozenset(
    {
        "text/uri-list",
        "x-special/gnome-copied-files",
        "application/x-kde-cutselection",
    }
)
_EMPTY_CLIPBOARD_HINTS = (
    "nothing is copied",
    "target targets not available",
    "target string not available",
    "clipboard is empty",
    "no selection",
)
_PREFERRED_IMAGE_MIMES = (
    "image/png",
    "image/webp",
    "image/jpeg",
    "image/bmp",
    "image/gif",
    "image/tiff",
)


@dataclass(frozen=True)
class ClipboardSnapshot:
    kind: Literal["empty", "text", "binary", "unsupported", "failed"]
    text: str = ""
    mime: str = ""
    data: bytes = b""


class Clipboard(Protocol):
    def write(self, text: str) -> str | None: ...

    def read(self) -> str: ...


def _is_text_target(target: str) -> bool:
    low = target.strip().lower()
    return low in _TEXT_TARGETS or low.startswith("text/plain")


def _pick_image_mime(targets: list[str]) -> str | None:
    by_lower = {t.strip().lower(): t.strip() for t in targets if t.strip()}
    for preferred in _PREFERRED_IMAGE_MIMES:
        if preferred in by_lower:
            return by_lower[preferred]
    for low, orig in by_lower.items():
        if low.startswith("image/"):
            return orig
    return None


class SystemClipboard:
    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        environ: Mapping[str, str] | None = None,
        popen=subprocess.Popen,
        run=subprocess.run,
    ) -> None:
        self._which = which
        self._env = os.environ if environ is None else environ
        self._popen = popen
        self._run = run

    def _wayland(self) -> bool:
        return self._env.get("XDG_SESSION_TYPE") == "wayland"

    def _probe_targets(self) -> tuple[Literal["ok", "empty", "failed"], list[str]]:
        """探测当前剪贴板可用 MIME / TARGETS，区分 `ok`、`empty` 与 `failed`。"""
        cmds: list[list[str]] = []
        if self._wayland() and self._which("wl-paste"):
            cmds.append(["wl-paste", "--list-types"])
        if self._which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
        if not cmds:
            return "failed", []

        saw_empty = False
        for cmd in cmds:
            try:
                result = self._run(cmd, capture_output=True, timeout=2)
            except Exception:
                continue
            if result.returncode == 0:
                raw = (
                    result.stdout.decode("utf-8", errors="replace")
                    if isinstance(result.stdout, (bytes, bytearray))
                    else str(result.stdout or "")
                )
                lines = [line.strip() for line in raw.splitlines() if line.strip()]
                if lines:
                    return "ok", lines
                saw_empty = True
            else:
                raw_err = getattr(result, "stderr", b"")
                err_text = (
                    raw_err.decode("utf-8", errors="replace")
                    if isinstance(raw_err, (bytes, bytearray))
                    else str(raw_err or "")
                ).lower()
                if any(hint in err_text for hint in _EMPTY_CLIPBOARD_HINTS):
                    saw_empty = True
        return ("empty", []) if saw_empty else ("failed", [])

    def list_targets(self) -> list[str]:
        """枚举当前剪贴板可用 MIME / TARGETS。"""
        status, targets = self._probe_targets()
        return targets if status == "ok" else []

    def write(self, text: str) -> str | None:
        """写入纯文本剪贴板。成功返回 None，失败返回用户可读原因。"""
        if self._which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif self._which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        elif self._wayland() and self._which("wl-copy"):
            cmd = ["wl-copy"]
        else:
            return "剪贴板工具缺失，请安装 xclip"
        return self._pipe_bytes(cmd, text.encode("utf-8"))

    def write_binary(self, data: bytes, mime: str = "image/png") -> str | None:
        """按指定 MIME 类型写入二进制剪贴板（如 image/png）。"""
        if self._which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-t", mime]
        elif self._wayland() and self._which("wl-copy"):
            cmd = ["wl-copy", "--type", mime]
        else:
            return "二进制剪贴板写入工具缺失，请安装 xclip"
        return self._pipe_bytes(cmd, data)

    def _pipe_bytes(self, cmd: list[str], payload: bytes) -> str | None:
        proc = self._popen(cmd, stdin=subprocess.PIPE)
        try:
            proc.communicate(payload, timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "剪贴板写入超时"
        if proc.returncode != 0:
            return "剪贴板写入失败"
        return None

    def read_binary(self, mime: str) -> bytes:
        """按指定 MIME 类型读取原始字节。读不到返回空字节。"""
        cmds: list[list[str]] = []
        if self._wayland() and self._which("wl-paste"):
            cmds.append(["wl-paste", "--type", mime])
        if self._which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard", "-t", mime, "-o"])
        for cmd in cmds:
            try:
                result = self._run(cmd, capture_output=True, timeout=2)
                if result.returncode == 0 and result.stdout:
                    return bytes(result.stdout) if isinstance(result.stdout, (bytes, bytearray)) else result.stdout.encode("utf-8")
            except Exception:
                continue
        return b""

    def _read_text_status(self) -> tuple[Literal["ok", "empty", "failed"], str]:
        if self._which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        elif self._which("xsel"):
            cmd = ["xsel", "--clipboard", "--output"]
        elif self._wayland() and self._which("wl-paste"):
            cmd = ["wl-paste", "--no-newline"]
        else:
            return "failed", ""
        try:
            result = self._run(cmd, capture_output=True, timeout=2)
        except Exception:
            return "failed", ""
        if result.returncode == 0:
            if isinstance(result.stdout, (bytes, bytearray)):
                text = result.stdout.decode("utf-8", errors="replace")
            else:
                text = str(result.stdout or "")
            return ("ok", text) if text else ("empty", "")
        raw_err = getattr(result, "stderr", b"")
        err_text = (
            raw_err.decode("utf-8", errors="replace")
            if isinstance(raw_err, (bytes, bytearray))
            else str(raw_err or "")
        ).lower()
        if any(hint in err_text for hint in _EMPTY_CLIPBOARD_HINTS):
            return "empty", ""
        return "failed", ""

    def read(self) -> str:
        """读取当前剪贴板文本内容。读不到返回空串。"""
        status, text = self._read_text_status()
        return text if status == "ok" else ""

    def snapshot(self) -> ClipboardSnapshot:
        """无损快照当前剪贴板：区分 `empty`、`text`、`binary` 与不可恢复的 `unsupported`/`failed`。"""
        status, targets = self._probe_targets()
        if status == "ok":
            lowered = {t.strip().lower() for t in targets if t.strip()}
            if lowered & _UNSUPPORTED_TARGETS:
                return ClipboardSnapshot(kind="unsupported")
            has_text = any(_is_text_target(t) for t in targets)
            img_mime = _pick_image_mime(targets)
            if img_mime and not has_text:
                raw = self.read_binary(img_mime)
                if raw:
                    return ClipboardSnapshot(kind="binary", mime=img_mime, data=raw)
                return ClipboardSnapshot(kind="failed")
            if not has_text:
                content_targets = [t for t in targets if t.strip().lower() not in _META_TARGETS]
                return ClipboardSnapshot(kind="unsupported" if content_targets else "empty")
            text_status, text = self._read_text_status()
            if text_status == "ok":
                return ClipboardSnapshot(kind="text", text=text)
            if text_status == "empty":
                return ClipboardSnapshot(kind="empty")
            return ClipboardSnapshot(kind="failed")

        if status == "empty":
            return ClipboardSnapshot(kind="empty")

        text_status, text = self._read_text_status()
        if text_status == "ok":
            return ClipboardSnapshot(kind="text", text=text)
        if text_status == "empty":
            return ClipboardSnapshot(kind="empty")
        return ClipboardSnapshot(kind="failed")

    def restore_snapshot(self, snap: ClipboardSnapshot) -> str | None:
        """从快照恢复原剪贴板；遇不可恢复格式或快照失败时不写空覆盖用户剪贴板。"""
        if snap.kind in ("unsupported", "failed"):
            return None
        if snap.kind == "binary":
            if snap.data and snap.mime:
                return self.write_binary(snap.data, snap.mime)
            return None
        if snap.kind == "text":
            return self.write(snap.text)
        if snap.kind == "empty":
            return self.write("")
        return None
