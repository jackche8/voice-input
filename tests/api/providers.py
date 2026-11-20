"""测试用能力提供者。明确不是识别引擎，不拼接字幕，不宣称官方 sidecar 可用。"""
from __future__ import annotations

import io
import wave

from _contracts import (
    ErrorCode,
    LogicError,
    OcrTextBlock,
    OcrTextLine,
    RecognizeSessionCreate,
    SessionFinalEvent,
    SttResponse,
)
from _ocr_text import join_block_texts


def make_wav_bytes(
    pcm_payload: bytes,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """生成合法 PCM WAV 字节，供 API 路由通过真实容器解码与静音校验。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_payload)
    return buf.getvalue()


def make_text_wav(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) % 2 != 0:
        raw += b" "
    return make_wav_bytes(raw)


class FixtureAsrProvider:
    """会话流式夹具：finish 返回预设文本，不把 PCM 当字符串拼接。"""

    official_engine = False

    def __init__(self, final_text: str = "fixture-final", language: str = "zh") -> None:
        self.final_text = final_text
        self.language = language
        self.started: dict[str, RecognizeSessionCreate] = {}
        self.chunks: dict[str, list[int]] = {}
        self.cancelled: set[str] = set()

    def start(self, session_id: str, body: RecognizeSessionCreate, *, sink=None) -> None:
        del sink
        self.started[session_id] = body
        self.chunks[session_id] = []

    def push_pcm(self, session_id: str, index: int, pcm: bytes) -> None:
        del pcm  # 夹具不解析、不拼接音频内容
        self.chunks.setdefault(session_id, []).append(index)

    def finish(self, session_id: str) -> SessionFinalEvent:
        return SessionFinalEvent(session_id=session_id, text=self.final_text, language=self.language)

    def cancel(self, session_id: str) -> None:
        self.cancelled.add(session_id)


def fixture_transcribe(audio: bytes, *, language: str | None = None, context: str | None = None) -> SttResponse:
    del context
    try:
        text = audio.decode("utf-8").strip()
        if text and text.isprintable() and "\x00" not in text:
            return SttResponse(text=text, language=language or "zh")
    except UnicodeDecodeError:
        pass
    return SttResponse(text="fixture-transcript", language=language or "zh")


def fixture_ocr(image_bytes: bytes, **_kwargs) -> str:
    if image_bytes == b"empty-image":
        return "  "
    try:
        return image_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "fixture-ocr-text"


def fixture_ocr_layout(
    image_bytes: bytes, **_kwargs
) -> tuple[int, int, str, list[OcrTextBlock]]:
    if image_bytes == b"empty-image":
        raise LogicError(ErrorCode.ocr_failed, "没有识别到文字")  # 与 extract_ocr_layout 同一契约
    try:
        raw_text = image_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        raw_text = "fixture-ocr-text"
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    blocks = [
        OcrTextBlock(
            bbox=(12, 16 + idx * 36, 200, 28),
            lines=[OcrTextLine(bbox=(12, 16 + idx * 36, 200, 28), text=line)],
            ocr_text=line,
            translated_text="",
            bg_color="#111827" if idx % 2 else "#ffffff",
            fg_color="#f8fafc" if idx % 2 else "#0f172a",
        )
        for idx, line in enumerate(lines)
    ]
    return 320, 180, join_block_texts([block.ocr_text for block in blocks]), blocks
