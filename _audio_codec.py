"""音频容器统一解码与静音检测（逻辑层原子能力）。

将手机/浏览器上传的压缩音频容器（webm/ogg/mp4/m4a/mp3/flac）及任意采样率/声道 WAV
统一解码并重采样为 16kHz 单声道 s16le 裸 PCM。
严禁将无法识别或已损坏的压缩容器当成裸 PCM 透传。
"""
from __future__ import annotations

import io
import math
import wave

from _contracts import ErrorCode, LogicError

TARGET_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


def _normalize_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _looks_like_container(raw: bytes) -> bool:
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return True
    if raw.startswith((b"\x1a\x45\xdf\xa3", b"OggS", b"fLaC", b"ID3")):
        return True
    if len(raw) >= 8 and raw[4:8] == b"ftyp":
        return True
    return False


def _try_fast_wav_16k_mono_s16(raw: bytes) -> bytes | None:
    """16kHz 单声道 16-bit PCM WAV 快路径；其他规格返回 None 交由 PyAV 重采样。"""
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            if (
                handle.getcomptype() == "NONE"
                and handle.getnchannels() == 1
                and handle.getsampwidth() == BYTES_PER_SAMPLE
                and handle.getframerate() == TARGET_SAMPLE_RATE
            ):
                frames = handle.readframes(handle.getnframes())
                if not frames:
                    raise LogicError(ErrorCode.invalid_request, "WAV 容器中无有效音频采样")
                if len(frames) % BYTES_PER_SAMPLE != 0:
                    raise LogicError(ErrorCode.invalid_request, "WAV 采样字节长度不完整")
                return frames
    except LogicError:
        raise
    except wave.Error:
        # 例如 IEEE float WAV (format tag 3) 或扩展块由 PyAV 进一步判定解码或报错
        return None
    return None


def decode_audio_to_pcm16k(
    raw_bytes: bytes,
    *,
    content_type: str | None = None,
) -> bytes:
    """解码音频字节为 16kHz mono s16le 裸 PCM。

    - 仅当显式指定 `content_type="audio/pcm"`（且非容器头）时允许直接接收偶数长度裸 PCM；
    - 16kHz mono s16 WAV 走标准库快路径；
    - 其余容器（webm/ogg/mp4/m4a/mp3/flac/非标 WAV）统一经 PyAV 解码与重采样；
    - 空字节、无音轨或损坏容器一律显式抛 `LogicError(ErrorCode.invalid_request)`。
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)) or len(raw_bytes) == 0:
        raise LogicError(ErrorCode.invalid_request, "音频数据不能为空")
    raw = bytes(raw_bytes)
    ct = _normalize_content_type(content_type)

    if ct == "audio/pcm" and not _looks_like_container(raw):
        if len(raw) % BYTES_PER_SAMPLE != 0:
            raise LogicError(
                ErrorCode.invalid_request,
                "audio/pcm 裸字节长度必须为偶数（16-bit s16le）",
                details={"byte_length": len(raw)},
            )
        return raw

    fast_pcm = _try_fast_wav_16k_mono_s16(raw)
    if fast_pcm is not None:
        return fast_pcm

    try:
        import av
    except ImportError as exc:  # pragma: no cover
        raise LogicError(
            ErrorCode.dependency_failed, f"音频解码依赖 PyAV 不可用: {exc}"
        ) from exc

    chunks: list[bytes] = []
    try:
        with av.open(io.BytesIO(raw), mode="r") as container:
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if not audio_streams:
                raise LogicError(ErrorCode.invalid_request, "媒体容器中不包含音频流")
            resampler = av.AudioResampler(
                format="s16",
                layout="mono",
                rate=TARGET_SAMPLE_RATE,
            )
            for frame in container.decode(audio=0):
                frame.pts = None
                for out_frame in resampler.resample(frame):
                    # 使用 to_ndarray() 避免 FFmpeg plane 行对齐填充字节混入 PCM
                    chunks.append(out_frame.to_ndarray().tobytes())
            for out_frame in resampler.resample(None):
                chunks.append(out_frame.to_ndarray().tobytes())
    except LogicError:
        raise
    except Exception as exc:
        raise LogicError(
            ErrorCode.invalid_request,
            f"无法解码音频容器（仅显式 audio/pcm 接受裸 PCM）: {exc}",
        ) from exc

    pcm = b"".join(chunks)
    if not pcm:
        raise LogicError(ErrorCode.invalid_request, "音频容器解码后无有效采样数据")
    return pcm


def pcm16le_rms(pcm_bytes: bytes) -> float:
    """计算 16-bit s16le PCM 的归一化均方根振幅（范围 [0.0, 1.0]）。"""
    if not isinstance(pcm_bytes, (bytes, bytearray, memoryview)):
        raise LogicError(ErrorCode.invalid_request, "PCM 输入必须是字节序列")
    if len(pcm_bytes) == 0:
        return 0.0
    if len(pcm_bytes) % BYTES_PER_SAMPLE != 0:
        raise LogicError(
            ErrorCode.invalid_request,
            "PCM s16le 字节长度必须为偶数",
            details={"byte_length": len(pcm_bytes)},
        )
    import numpy as np

    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float64) / 32768.0
    mean_sq = float(np.mean(samples * samples))
    return math.sqrt(mean_sq) if mean_sq > 0.0 else 0.0


def is_pcm16le_silent(pcm_bytes: bytes, *, rms_threshold: float = 0.003) -> bool:
    """按归一化 RMS 阈值判定 s16le PCM 是否为静音。"""
    if rms_threshold < 0.0:
        raise LogicError(ErrorCode.invalid_request, "rms_threshold 不能为负")
    return pcm16le_rms(pcm_bytes) < rms_threshold


__all__ = [
    "BYTES_PER_SAMPLE",
    "TARGET_SAMPLE_RATE",
    "decode_audio_to_pcm16k",
    "is_pcm16le_silent",
    "pcm16le_rms",
]
