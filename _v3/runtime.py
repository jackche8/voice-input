"""显式运行时依赖与沙箱组合。禁止无参默认指向 live 配置/历史/队列。

生产组合在 _v3/production.py，必须显式给出路径与令牌。
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from _audio_codec import decode_audio_to_pcm16k, is_pcm16le_silent
from _config import ConfigStore
from _contracts import (
    DEFAULT_TRANSLATE_LANG_PAIR,
    AppConfig,
    AudioInputListResponse,
    ErrorCode,
    LogicError,
    OcrTextBlock,
    PolishRequest,
    PolishResponse,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
    validate_direction_override,
)
from _events import EventBus
from _history import HistoryStore
from _history_service import HistoryService
from _jobs import JobStore
from _jobs_service import JobsService
from _ocr_text import block_segments, block_translated_text
from _paths import default_app_paths
from _ports import DEFAULT_SERVER_PORT
from _session_finals import SessionFinalIndex
from _translate import has_translatable_language
from _utterance import UtteranceService
from _v3.auth import AuthSettings
from _v3.component_routes import ComponentRegistry
from _v3.lan_access import LanAccessService
from _v3.sessions import StreamingAsrProvider
from _v3.setup_service import SetupService

TranscribeFn = Callable[..., SttResponse]
PolishFn = Callable[[PolishRequest], PolishResponse]
TranslateFn = Callable[[TranslateRequest], TranslateResponse]
TranslateBlocksFn = Callable[..., list[OcrTextBlock]]
OcrFn = Callable[..., str]
OcrLayoutFn = Callable[..., tuple[int, int, str, list[OcrTextBlock]]]
DecodeAudioFn = Callable[..., bytes]
ListAudioInputsFn = Callable[[], AudioInputListResponse]


def _default_translate_blocks(translate: TranslateFn) -> TranslateBlocksFn:
    def _run(
        blocks: list[OcrTextBlock],
        *,
        source_lang: str,
        target_lang: str,
        lang_pair: str = DEFAULT_TRANSLATE_LANG_PAIR,
    ) -> list[OcrTextBlock]:
        try:
            norm_pair, override = validate_direction_override(lang_pair, source_lang, target_lang)
        except ValueError as exc:
            raise LogicError(ErrorCode.invalid_request, str(exc)) from exc
        if override is None:
            raise LogicError(ErrorCode.invalid_request, "translate_blocks 必须显式指定 source_lang 与 target_lang")
        norm_source, norm_target = override
        translated: list[OcrTextBlock] = []
        for block in blocks:  # 与 _translate.translate_blocks 同一翻译单位：每个硬行段一项，跳纯符号项并按原缩进拼回
            texts = [
                translate(
                    TranslateRequest(
                        text=seg.text,
                        lang_pair=norm_pair,
                        source_lang=norm_source,
                        target_lang=norm_target,
                    )
                ).text
                if has_translatable_language(seg.text)
                else seg.text
                for seg in block_segments(block.lines)
            ]
            translated.append(block.model_copy(update={"translated_text": block_translated_text(block.lines, texts)}))
        return translated

    return _run


def decode_uploaded_audio(
    runtime: "Runtime",
    payload: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
) -> bytes:
    """统一解码上传音频容器为 16kHz mono s16le PCM 并执行 RMS 静音检测。"""
    if not payload:
        raise LogicError(ErrorCode.invalid_request, "音频不能为空")
    base_ct = (content_type or "").split(";", 1)[0].strip().lower()
    effective_ct = content_type
    if base_ct in ("", "application/octet-stream"):
        fname = (filename or "").strip().lower()
        if fname.endswith((".pcm", ".raw", ".s16le")):
            effective_ct = "audio/pcm"
    pcm = runtime.decode_audio(payload, content_type=effective_ct)
    if not pcm:
        raise LogicError(ErrorCode.invalid_request, "音频不能为空")
    if is_pcm16le_silent(pcm):
        raise LogicError(ErrorCode.invalid_request, "音频为静音，无法转写")
    return pcm


def _live_roots() -> tuple[Path, ...]:
    live = default_app_paths(os.environ)
    return tuple(p.resolve() for p in (live.config_root, live.data_root))


def assert_sandbox_path(path: Path) -> Path:
    resolved = path.resolve()
    for root in _live_roots():
        if resolved == root or resolved.is_relative_to(root):
            raise RuntimeError(f"拒绝绑定 live 路径: {resolved}")
    return resolved


@dataclass
class Runtime:
    config: ConfigStore
    history: HistoryService
    jobs: JobsService
    events: EventBus
    finals: SessionFinalIndex
    utterance: UtteranceService
    auth: AuthSettings
    transcribe: TranscribeFn | None = None
    polish: PolishFn | None = None
    translate: TranslateFn | None = None
    translate_blocks: TranslateBlocksFn | None = None
    ocr: OcrFn | None = None
    ocr_layout: OcrLayoutFn | None = None
    decode_audio: DecodeAudioFn = decode_audio_to_pcm16k
    stream_provider: StreamingAsrProvider | None = None
    setup: SetupService | None = None
    lan: LanAccessService | None = None
    list_audio_inputs: ListAudioInputsFn | None = None
    components: ComponentRegistry = field(default_factory=ComponentRegistry)
    max_upload_bytes: int = 15 * 1024 * 1024
    max_chunk_bytes: int = 160_000
    max_session_pcm_bytes: int = 16_000 * 2 * 120
    upload_tmp: Path | None = None
    sse_queue_max: int = 32
    session_retain_closed_s: float = 60.0
    session_idle_open_s: float = 180.0
    clock: Callable[[], float] = time.monotonic


def build_sandbox_runtime(
    *,
    root: Path,
    token: str,
    transcribe: TranscribeFn | None = None,
    polish: PolishFn | None = None,
    translate: TranslateFn | None = None,
    translate_blocks: TranslateBlocksFn | None = None,
    ocr: OcrFn | None = None,
    ocr_layout: OcrLayoutFn | None = None,
    decode_audio: DecodeAudioFn | None = None,
    stream_provider: StreamingAsrProvider | None = None,
    setup: SetupService | None = None,
    lan_ips: Callable[[], list[str]] | None = None,
    list_audio_inputs: ListAudioInputsFn | None = None,
    port: int = DEFAULT_SERVER_PORT,
    polish_enabled: bool = False,
    max_upload_bytes: int | None = None,
    max_chunk_bytes: int | None = None,
    sse_queue_max: int | None = None,
    session_retain_closed_s: float | None = None,
    session_idle_open_s: float | None = None,
    clock: Callable[[], float] | None = None,
) -> Runtime:
    """测试/闸门专用。必须给出 sandbox 根目录与假 token，不会回落到 live 路径。"""
    if not token or not str(token).strip():
        raise ValueError("sandbox 必须显式提供测试 token，且不得读取 live token")
    lowered = str(token).lower()
    if "live" in lowered or lowered.startswith("prod"):
        raise ValueError("拒绝看起来像生产用途的 token 名称")
    root = assert_sandbox_path(Path(root))
    root.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(assert_sandbox_path(root / "config.json"))
    config.save(AppConfig(ollama_model="qwen3.5:4b", polish_enabled=polish_enabled))
    events = EventBus()
    history = HistoryService(HistoryStore(assert_sandbox_path(root / "history.jsonl")), environ={})
    jobs = JobsService(JobStore(assert_sandbox_path(root / "jobs.json")), events, claim_lease_s=60.0)
    finals = SessionFinalIndex()
    utterance = UtteranceService(
        jobs=jobs,
        history=history,
        events=events,
        config=config,
        finals=finals,
        polish=polish,
        transcribe=transcribe,
    )
    upload_tmp = assert_sandbox_path(root / "upload-tmp")
    upload_tmp.mkdir(parents=True, exist_ok=True)
    effective_translate_blocks = (
        translate_blocks
        if translate_blocks is not None
        else (_default_translate_blocks(translate) if translate is not None else None)
    )
    return Runtime(
        config=config,
        history=history,
        jobs=jobs,
        events=events,
        finals=finals,
        utterance=utterance,
        auth=AuthSettings(token=token, port=port),
        transcribe=transcribe,
        polish=polish,
        translate=translate,
        translate_blocks=effective_translate_blocks,
        ocr=ocr,
        ocr_layout=ocr_layout,
        decode_audio=decode_audio_to_pcm16k if decode_audio is None else decode_audio,
        stream_provider=stream_provider,
        setup=setup,
        lan=None if lan_ips is None else LanAccessService(token=token, list_ips=lan_ips, port=port),
        list_audio_inputs=list_audio_inputs,
        max_upload_bytes=15 * 1024 * 1024 if max_upload_bytes is None else max_upload_bytes,
        max_chunk_bytes=160_000 if max_chunk_bytes is None else max_chunk_bytes,
        upload_tmp=upload_tmp,
        sse_queue_max=32 if sse_queue_max is None else sse_queue_max,
        session_retain_closed_s=60.0 if session_retain_closed_s is None else session_retain_closed_s,
        session_idle_open_s=180.0 if session_idle_open_s is None else session_idle_open_s,
        clock=time.monotonic if clock is None else clock,
    )
