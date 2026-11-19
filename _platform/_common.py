"""跨平台适配层公共原子能力（Logic 层，严禁依赖 `_linux.*` 或任何平台专属库）。

提供：
- `png_dimensions`：无 PIL 读取 PNG 文件头宽高；
- `SoundDeviceSource` & `play_beep`：跨平台线程安全麦克风源与提示音（共享 `_AUDIO_LOCK`）；
- `check_focus_and_self_guard`：三端统一的焦点/自窗口注入护栏；
- `probe_ocr_capabilities`：三端统一的 OCR 语言包能力自检。
"""
from __future__ import annotations

import struct
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from _capture import SAMPLE_RATE, PcmSink
from _contracts import SetupCapability
from _platform.protocol import FocusTarget, PasteResult

DEFAULT_BEEP_SAMPLE_RATE = SAMPLE_RATE
_AUDIO_LOCK = threading.Lock()
_BEEP_LOCK = threading.Lock()


class SoundDeviceSource:
    """跨平台 16k 单声道 int16 麦克风输入流；与 `play_beep` 共享 `_AUDIO_LOCK` 避免 PortAudio 冲突。"""

    def __init__(self, *, samplerate: int = SAMPLE_RATE, sounddevice_mod=None) -> None:
        self.samplerate = samplerate
        self._sd = sounddevice_mod
        self._stream = None

    def _module(self):
        if self._sd is not None:
            return self._sd
        import sounddevice as sd

        return sd

    def start(self, sink: PcmSink) -> None:
        sd = self._module()

        def callback(indata, frames, time_info, status):
            del frames, time_info, status
            sink(bytes(indata))

        with _AUDIO_LOCK:
            stream = sd.RawInputStream(
                samplerate=self.samplerate, channels=1, dtype="int16", callback=callback
            )
            try:
                stream.start()
            except Exception:
                try:
                    stream.close()
                except Exception:
                    pass
                raise
            self._stream = stream

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        # 流关闭异常（设备热拔 / PipeWire 重启）绝不能向上抛：否则调用方其后的修饰键释放被跳过
        try:
            with _AUDIO_LOCK:
                stream.stop()
                stream.close()
        except Exception as exc:
            print(f"[mic] 关闭音频流异常: {exc}", file=sys.stderr)


def noop() -> None:
    return None


def always_false() -> bool:
    return False


def png_dimensions(path: str | Path) -> tuple[int, int]:
    """从 PNG 文件头读取图片宽高（无需 PIL），无效 PNG 文件头显式抛出 ValueError。"""
    with open(path, "rb") as fh:
        header = fh.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"非有效 PNG 文件头: {path}")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def play_beep(
    freq: float = 440,
    duration: float = 0.1,
    volume: float = 0.3,
    *,
    background: bool = True,
    sounddevice_mod=None,
    sample_rate: int = DEFAULT_BEEP_SAMPLE_RATE,
) -> None:
    """跨平台线程安全提示音（基于 `sounddevice` + `numpy` + 串行锁，不依赖 `_linux`）。"""

    def _worker() -> None:
        try:
            import numpy as np

            sd = sounddevice_mod
            if sd is None:
                import sounddevice as sd

            frames = max(1, int(sample_rate * duration))
            t = np.linspace(0, duration, frames, endpoint=False)
            wave = volume * np.sin(2 * np.pi * freq * t).astype(np.float32)
            fade = min(int(sample_rate * 0.01), len(wave) // 4)
            if fade > 0:
                wave[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
                wave[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
            with _BEEP_LOCK:
                with _AUDIO_LOCK:
                    stop_fn = getattr(sd, "stop", None)
                    if callable(stop_fn):
                        try:
                            stop_fn()
                        except Exception:
                            pass
                    sd.play(wave, sample_rate)
                wait_fn = getattr(sd, "wait", None)
                if callable(wait_fn):
                    try:
                        wait_fn()
                    except Exception:
                        pass
        except Exception:
            pass  # 提示音属非关键辅助反馈，失败不中断主链路

    if background:
        threading.Thread(target=_worker, daemon=True).start()
    else:
        _worker()


def check_focus_and_self_guard(
    target: FocusTarget | None,
    live: FocusTarget | None = None,
) -> PasteResult | None:
    """公共焦点/自窗口注入护栏：命中阻断返回 `PasteResult(ok=False, uncertain=False)`，放行返回 `None`。"""
    if target is not None and target.is_self:
        return PasteResult(
            ok=False,
            method="self_window_blocked",
            detail="目标焦点为自身窗口，已拒绝注入",
            uncertain=False,
        )
    if live is None:
        return None
    if live.is_self:
        return PasteResult(
            ok=False,
            method="self_window_blocked",
            detail="当前焦点位于自身窗口，已拒绝注入",
            uncertain=False,
        )
    if target is not None and target.focus_id and live.focus_id and target.focus_id != live.focus_id:
        return PasteResult(
            ok=False,
            method="focus_mismatch",
            detail="焦点与任务绑定时不一致，未注入",
            uncertain=False,
        )
    return None


def probe_ocr_capabilities(
    ocr_languages: Callable[[], Iterable[str]] | None,
    *,
    missing_prefix: str = "tesseract 缺少",
) -> list[SetupCapability]:
    """公共 OCR 语言包能力自检（chi_sim / eng）。"""
    if ocr_languages is None:
        return []
    try:
        langs = set(ocr_languages())
        err: str | None = None
    except Exception as exc:
        langs, err = set(), str(exc)
    caps: list[SetupCapability] = []
    for code, label in (("chi_sim", "OCR 简体中文"), ("eng", "OCR 英文")):
        caps.append(
            SetupCapability(
                id=f"ocr_{code}",
                label=label,
                available=code in langs,
                required=True,
                detail=err or (None if code in langs else f"{missing_prefix} {code} 语言包"),
            )
        )
    return caps
