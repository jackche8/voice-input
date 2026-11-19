"""_audio_codec 容器解码、重采样、静音检测与损坏容器拦截单测。"""
from __future__ import annotations

import io
import struct
import wave

import av
import numpy as np
import pytest

from _audio_codec import (
    TARGET_SAMPLE_RATE,
    decode_audio_to_pcm16k,
    is_pcm16le_silent,
    pcm16le_rms,
)
from _contracts import ErrorCode, LogicError


def _encode_container(
    fmt: str,
    codec: str,
    *,
    rate: int = 48000,
    stereo: bool = True,
    duration_s: float = 0.4,
    amplitude: float = 0.5,
) -> bytes:
    n = int(rate * duration_s)
    t = np.linspace(0.0, duration_s, n, endpoint=False, dtype=np.float32)
    mono = (amplitude * np.sin(2.0 * np.pi * 440.0 * t) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    layout = "stereo" if stereo else "mono"
    with av.open(buf, mode="w", format=fmt) as out:
        stream = out.add_stream(codec, rate=rate, layout=layout)
        if stereo:
            interleaved = np.ascontiguousarray(np.stack([mono, mono], axis=1).reshape(1, -1))
            frame = av.AudioFrame.from_ndarray(interleaved, format="s16", layout="stereo")
        else:
            frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for pkt in stream.encode(frame):
            out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    return buf.getvalue()


def _make_wav(
    *,
    rate: int = 16000,
    channels: int = 1,
    duration_s: float = 0.25,
    amplitude: float = 0.4,
) -> bytes:
    n = int(rate * duration_s)
    t = np.linspace(0.0, duration_s, n, endpoint=False, dtype=np.float32)
    sig = (amplitude * np.sin(2.0 * np.pi * 440.0 * t) * 32767.0).astype("<i2")
    if channels > 1:
        sig = np.repeat(sig[:, None], channels, axis=1).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(sig.tobytes())
    return buf.getvalue()


@pytest.mark.parametrize(
    ("fmt", "codec", "rate", "stereo", "mime"),
    [
        ("webm", "libopus", 48000, True, "audio/webm;codecs=opus"),
        ("ogg", "libopus", 48000, False, "audio/ogg"),
        ("mp4", "aac", 44100, True, "audio/mp4"),
        ("ipod", "aac", 44100, False, "audio/x-m4a"),
        ("mp3", "libmp3lame", 44100, True, "audio/mpeg"),
        ("flac", "flac", 48000, True, "audio/flac"),
    ],
)
def test_decode_compressed_containers_to_16k_mono_s16le(
    fmt: str, codec: str, rate: int, stereo: bool, mime: str
):
    raw = _encode_container(fmt, codec, rate=rate, stereo=stereo, duration_s=0.4, amplitude=0.5)
    pcm = decode_audio_to_pcm16k(raw, content_type=mime)
    assert len(pcm) > 0 and len(pcm) % 2 == 0
    samples = len(pcm) // 2
    expected = int(TARGET_SAMPLE_RATE * 0.4)
    # MP3/AAC 编解码器自带少量帧延迟，采样点数应在合理误差范围内
    assert abs(samples - expected) <= 1600
    assert pcm16le_rms(pcm) > 0.2
    assert not is_pcm16le_silent(pcm)


def test_wav_fast_path_and_48k_stereo_resample():
    wav_16k = _make_wav(rate=16000, channels=1, duration_s=0.25, amplitude=0.5)
    pcm_fast = decode_audio_to_pcm16k(wav_16k, content_type="audio/wav")
    assert len(pcm_fast) == int(16000 * 0.25) * 2
    assert pcm16le_rms(pcm_fast) > 0.3

    wav_48k_stereo = _make_wav(rate=48000, channels=2, duration_s=0.25, amplitude=0.5)
    pcm_resampled = decode_audio_to_pcm16k(wav_48k_stereo, content_type="audio/wav")
    assert abs(len(pcm_resampled) // 2 - int(16000 * 0.25)) <= 32
    assert pcm16le_rms(pcm_resampled) > 0.3


def test_explicit_audio_pcm_and_disguised_container_guard():
    raw_pcm = struct.pack("<4h", 1000, -1000, 2000, -2000)
    assert decode_audio_to_pcm16k(raw_pcm, content_type="audio/pcm;rate=16000") == raw_pcm

    with pytest.raises(LogicError) as odd_err:
        decode_audio_to_pcm16k(b"\x01\x02\x03", content_type="audio/pcm")
    assert odd_err.value.code == ErrorCode.invalid_request

    # 即使调用方误标 audio/pcm，压缩容器头也不得当裸 PCM 原样放行
    webm = _encode_container("webm", "libopus", duration_s=0.3)
    decoded_webm = decode_audio_to_pcm16k(webm, content_type="audio/pcm")
    assert decoded_webm != webm
    assert pcm16le_rms(decoded_webm) > 0.2


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (b"", None),
        (b"", "audio/pcm"),
        (b"\x01\x00" * 200, None),
        (b"\x01\x00" * 200, "application/octet-stream"),
        (b"\x1a\x45\xdf\xa3corrupted-webm-bytes", "audio/webm"),
        (b"\x1a\x45\xdf\xa3corrupted-webm-bytes", "audio/pcm"),
        (b"RIFF\x24\x00\x00\x00WAVEcorrupt", "audio/wav"),
        (b"OggScorrupted", "audio/ogg"),
    ],
)
def test_empty_or_corrupt_or_unlabeled_raw_bytes_rejected(
    payload: bytes, content_type: str | None
):
    with pytest.raises(LogicError) as ei:
        decode_audio_to_pcm16k(payload, content_type=content_type)
    assert ei.value.code == ErrorCode.invalid_request


def test_empty_wav_zero_frames_rejected():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"")
    with pytest.raises(LogicError) as ei:
        decode_audio_to_pcm16k(buf.getvalue(), content_type="audio/wav")
    assert ei.value.code == ErrorCode.invalid_request


def test_pcm16le_rms_and_silence_detection():
    assert pcm16le_rms(b"") == 0.0
    assert is_pcm16le_silent(b"") is True

    silence = b"\x00\x00" * 800
    assert pcm16le_rms(silence) == 0.0
    assert is_pcm16le_silent(silence) is True

    full_neg = struct.pack("<h", -32768) * 100
    assert pytest.approx(pcm16le_rms(full_neg), rel=1e-6) == 1.0
    assert is_pcm16le_silent(full_neg) is False

    # 振幅 50 / 32768 ≈ 0.00152 < 0.003 → 静音；振幅 300 / 32768 ≈ 0.00915 > 0.003 → 有声
    quiet = struct.pack("<h", 50) * 400
    audible = struct.pack("<h", 300) * 400
    assert is_pcm16le_silent(quiet, rms_threshold=0.003) is True
    assert is_pcm16le_silent(audible, rms_threshold=0.003) is False

    with pytest.raises(LogicError) as odd_err:
        pcm16le_rms(b"\x00\x01\x02")
    assert odd_err.value.code == ErrorCode.invalid_request

    with pytest.raises(LogicError) as neg_err:
        is_pcm16le_silent(silence, rms_threshold=-0.01)
    assert neg_err.value.code == ErrorCode.invalid_request
