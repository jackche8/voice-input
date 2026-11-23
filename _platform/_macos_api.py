"""macOS 底层系统接口：`screencapture`/`mss` + `lsappinfo`/`osascript` + `pbcopy`/`pbpaste` + Quartz `CGEvent`。

严禁依赖 `_linux.*`；临时截图与剪贴板中转文件一律落在调用方传入的 `temp_dir`（项目 `.pp/tmp` 下），绝不触碰 `/tmp`。
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from _hotkey import KEY_TABLE, MACOS_MODIFIERS
from _platform._common import png_dimensions
from _platform.protocol import (
    ScreenCapture,
    capture_screen_mss,
)

KVK_ANSI_V = KEY_TABLE["v"].macos_keycode
KCG_EVENT_FLAG_MASK_COMMAND = MACOS_MODIFIERS["super"]
KCG_HID_EVENT_TAP = 0


@dataclass(frozen=True)
class MacClipboardSnapshot:
    kind: Literal["empty", "text", "png", "unsupported", "failed"]
    text: str = ""
    png_bytes: bytes = b""


class MacOSSystemApi(Protocol):
    def get_current_pid(self) -> int: ...

    def frontmost_app(self) -> tuple[str, int, str]:
        """返回 `(bundle_id, pid, app_or_window_name)`。"""
        ...

    def capture_screen(self, temp_dir: Path) -> ScreenCapture: ...

    def read_clipboard_text(self) -> str: ...

    def write_clipboard_text(self, text: str) -> str | None: ...

    def write_clipboard_png(self, png: bytes, temp_dir: Path) -> str | None: ...

    def snapshot_clipboard(self, temp_dir: Path) -> MacClipboardSnapshot: ...

    def restore_clipboard(self, snap: MacClipboardSnapshot, temp_dir: Path) -> None: ...

    def post_cmd_v(self) -> bool: ...

    def is_accessibility_trusted(self) -> bool: ...

    def is_screen_recording_allowed(self) -> bool: ...


class RealMacOSSystemApi:
    """真机 macOS 系统接口封装（`screencapture`/`mss` + `lsappinfo`/`osascript` + `pbcopy` + `CGEvent`）。"""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen=subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        mss_factory=None,
    ) -> None:
        self._run = run
        self._popen = popen
        self._which = which
        self._mss_factory = mss_factory

    def get_current_pid(self) -> int:
        return os.getpid()

    def frontmost_app(self) -> tuple[str, int, str]:
        if self._which("lsappinfo"):
            try:
                front = self._run(["lsappinfo", "front"], capture_output=True, text=True, timeout=2, check=False)
                asn = (front.stdout or "").strip()
                if front.returncode == 0 and asn:
                    info = self._run(
                        ["lsappinfo", "info", "-only", "bundleid", "-only", "pid", "-only", "name", asn],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False,
                    )
                    if info.returncode == 0 and info.stdout:
                        m_bid = re.search(r'"CFBundleIdentifier"\s*=\s*"([^"]*)"', info.stdout)
                        m_pid = re.search(r'"pid"\s*=\s*(\d+)', info.stdout)
                        m_name = re.search(r'"LSDisplayName"\s*=\s*"([^"]*)"', info.stdout)
                        bid = m_bid.group(1) if m_bid else ""
                        pid = int(m_pid.group(1)) if m_pid else 0
                        name = m_name.group(1) if m_name else ""
                        if bid or pid:
                            return bid, pid, name
            except Exception:
                pass

        if self._which("osascript"):
            script = (
                'tell application "System Events"\n'
                "  set fp to first application process whose frontmost is true\n"
                '  return (bundle identifier of fp as text) & "\\t" & (unix id of fp as text) & "\\t" & (name of fp as text)\n'
                "end tell"
            )
            try:
                proc = self._run(["osascript", "-e", script], capture_output=True, text=True, timeout=3, check=False)
                if proc.returncode == 0 and proc.stdout:
                    parts = proc.stdout.strip().split("\t")
                    if len(parts) >= 2:
                        bid = parts[0].strip()
                        pid = int(parts[1].strip()) if parts[1].strip().isdigit() else 0
                        name = parts[2].strip() if len(parts) >= 3 else ""
                        return bid, pid, name
            except Exception:
                pass
        return "", 0, ""

    def capture_screen(self, temp_dir: Path) -> ScreenCapture:
        """优先用 `mss` 抓取全虚拟桌面；失败则回退 macOS 原生 `screencapture -x` 整屏抓取。"""
        try:
            return capture_screen_mss(self._mss_factory)
        except Exception as mss_err:
            if not self._which("screencapture"):
                raise RuntimeError(f"macOS 全屏抓屏不可用: {mss_err}") from mss_err
            temp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".macos-screen.", suffix=".png", dir=str(temp_dir))
            os.close(fd)
            shot_path = Path(tmp_name)
            try:
                proc = self._run(
                    ["screencapture", "-x", str(shot_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc.returncode != 0 or not shot_path.is_file() or shot_path.stat().st_size == 0:
                    raise RuntimeError(
                        f"screencapture 全屏截屏失败 (无录屏权限或已拒绝): {(proc.stderr or '').strip()}"
                    )
                png_bytes = shot_path.read_bytes()
                width, height = png_dimensions(shot_path)
                if width <= 0 or height <= 0:
                    raise RuntimeError(f"screencapture 全屏截屏图像尺寸非法: {(width, height)!r}")
                return ScreenCapture(png_bytes=png_bytes, virtual_rect=(0, 0, width, height))
            finally:
                shot_path.unlink(missing_ok=True)

    def _read_clipboard_text_status(self) -> tuple[bool, str]:
        if not self._which("pbpaste"):
            return False, ""
        env = dict(os.environ)
        env.setdefault("LANG", "en_US.UTF-8")
        try:
            proc = self._run(["pbpaste"], capture_output=True, timeout=2, check=False, env=env)
            if proc.returncode == 0:
                raw = proc.stdout
                text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
                return True, text
        except Exception:
            pass
        return False, ""

    def read_clipboard_text(self) -> str:
        ok, text = self._read_clipboard_text_status()
        return text if ok else ""

    def write_clipboard_text(self, text: str) -> str | None:
        if not self._which("pbcopy"):
            return "未找到 macOS pbcopy 工具"
        env = dict(os.environ)
        env.setdefault("LANG", "en_US.UTF-8")
        proc = self._popen(["pbcopy"], stdin=subprocess.PIPE, env=env)
        try:
            proc.communicate(text.encode("utf-8"), timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "pbcopy 写入超时"
        if proc.returncode != 0:
            return "pbcopy 写入失败"
        return None

    def snapshot_clipboard(self, temp_dir: Path) -> MacClipboardSnapshot:
        if self._which("osascript"):
            try:
                info = self._run(
                    ["osascript", "-e", "clipboard info"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except Exception:
                info = None

            if info is not None and info.returncode == 0:
                out = (info.stdout or "").strip()
                if not out:
                    return MacClipboardSnapshot(kind="empty")
                low_out = out.lower()
                has_file = ("furl" in low_out) or ("hfs" in low_out) or ("file url" in low_out)
                if has_file:
                    return MacClipboardSnapshot(kind="unsupported")

                has_png = "pngf" in low_out
                has_text = (
                    ("utf8" in low_out)
                    or ("ut16" in low_out)
                    or ("string" in low_out)
                    or ("unicode text" in low_out)
                )
                if has_png and not has_text:
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    fd, tmp_name = tempfile.mkstemp(prefix=".clip-snap.", suffix=".png", dir=str(temp_dir))
                    os.close(fd)
                    snap_path = Path(tmp_name)
                    try:
                        export_script = (
                            f'set f to open for access POSIX file "{snap_path}" with write permission\n'
                            "try\n"
                            "  set eof f to 0\n"
                            "  write (the clipboard as «class PNGf») to f\n"
                            "end try\n"
                            "close access f"
                        )
                        exp = self._run(
                            ["osascript", "-e", export_script],
                            capture_output=True,
                            text=True,
                            timeout=3,
                            check=False,
                        )
                        if exp.returncode == 0 and snap_path.is_file() and snap_path.stat().st_size > 0:
                            return MacClipboardSnapshot(kind="png", png_bytes=snap_path.read_bytes())
                        return MacClipboardSnapshot(kind="failed")
                    except Exception:
                        return MacClipboardSnapshot(kind="failed")
                    finally:
                        snap_path.unlink(missing_ok=True)

                if not has_text:
                    return MacClipboardSnapshot(kind="unsupported")

                ok, txt = self._read_clipboard_text_status()
                if not ok:
                    return MacClipboardSnapshot(kind="failed")
                return MacClipboardSnapshot(kind="text", text=txt) if txt else MacClipboardSnapshot(kind="empty")

        ok, txt = self._read_clipboard_text_status()
        if ok and txt:
            return MacClipboardSnapshot(kind="text", text=txt)
        return MacClipboardSnapshot(kind="failed")

    def write_clipboard_png(self, png: bytes, temp_dir: Path) -> str | None:
        """将 PNG 经应用临时目录（绝不写 `/tmp`）与 osascript 写入系统剪贴板。成功返回 None。"""
        if not png:
            return "剪贴板图片为空"
        if not self._which("osascript"):
            return "未找到 macOS osascript 工具"
        img_path: Path | None = None
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".clip-png.", suffix=".png", dir=str(temp_dir))
            os.close(fd)
            img_path = Path(tmp_name)
            img_path.write_bytes(png)
        except OSError as exc:
            if img_path is not None:
                img_path.unlink(missing_ok=True)
            return f"准备图片剪贴板临时文件失败: {exc}"
        try:
            escaped = str(img_path).replace("\\", "\\\\").replace('"', '\\"')
            script = f'set the clipboard to (read (POSIX file "{escaped}") as «class PNGf»)'
            try:
                proc = self._run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return "osascript 写入图片剪贴板超时"
            except OSError as exc:
                return f"osascript 写入图片剪贴板失败: {exc}"
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                return f"osascript 写入图片剪贴板失败: {detail}" if detail else "osascript 写入图片剪贴板失败"
            return None
        finally:
            img_path.unlink(missing_ok=True)

    def restore_clipboard(self, snap: MacClipboardSnapshot, temp_dir: Path) -> None:
        if snap.kind in ("unsupported", "failed"):
            return
        if snap.kind == "png":
            if snap.png_bytes:
                self.write_clipboard_png(snap.png_bytes, temp_dir)
            return
        if snap.kind == "text":
            self.write_clipboard_text(snap.text)
            return
        if snap.kind == "empty":
            self.write_clipboard_text("")

    def post_cmd_v(self) -> bool:
        try:
            app_svc = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            app_svc.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
            app_svc.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
            app_svc.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            app_svc.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            cf.CFRelease.argtypes = [ctypes.c_void_p]

            kvk_v = KEY_TABLE["v"].macos_keycode
            down = app_svc.CGEventCreateKeyboardEvent(None, kvk_v, True)
            up = app_svc.CGEventCreateKeyboardEvent(None, kvk_v, False)
            if down and up:
                try:
                    app_svc.CGEventSetFlags(down, KCG_EVENT_FLAG_MASK_COMMAND)
                    app_svc.CGEventSetFlags(up, KCG_EVENT_FLAG_MASK_COMMAND)
                    app_svc.CGEventPost(KCG_HID_EVENT_TAP, down)
                    app_svc.CGEventPost(KCG_HID_EVENT_TAP, up)
                    return True
                finally:
                    cf.CFRelease(down)
                    cf.CFRelease(up)
        except Exception:
            pass

        if self._which("osascript"):
            proc = self._run(
                ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return proc.returncode == 0
        return False

    def is_accessibility_trusted(self) -> bool:
        try:
            app_svc = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            app_svc.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(app_svc.AXIsProcessTrusted())
        except Exception:
            return False

    def is_screen_recording_allowed(self) -> bool:
        try:
            cg = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
        except Exception:
            return False
