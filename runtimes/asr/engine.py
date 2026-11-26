"""官方引擎双后端（Linux vLLM + 跨平台 Transformers）适配。

仅在 load() 时按需导入 qwen_asr / torch / vLLM，import 本模块不占 GPU。
"""
from __future__ import annotations

import gc
import importlib.util
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    ErrorCode,
    LogicError,
    SessionFinalEvent,
    SessionPartialEvent,
    SttResponse,
)
from runtimes.asr.asr_text import parse_asr_text
from runtimes.asr.protocol import (
    DEFAULT_BACKEND,
    DEFAULT_CHUNK_SIZE_SEC,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_MAX_MODEL_LEN,
    DEFAULT_MAX_SESSION_SAMPLES,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_UNFIXED_CHUNK_NUM,
    DEFAULT_UNFIXED_TOKEN_NUM,
    DEFAULT_VLLM_MAX_NUM_SEQS,
    OFFICIAL_PARTIAL_POLICY,
    SUPPORTED_BACKENDS,
    normalize_language,
)
from runtimes.asr.stream_step import (
    BoundedOfficialStream,
    StepReport,
    _clear_exception_frames,
    decode_whole,
    is_cuda_reclaim_target,
    numpy_pcm_ops,
    reclaim_cuda_cache,
)

MIN_INPUT_S = 0.5


class AsrEngine(Protocol):
    loaded: bool

    def transcribe(
        self, pcm: bytes, *, language: str | None = None, context: str | None = None
    ) -> SttResponse: ...

    def start_stream(
        self,
        session_id: str,
        *,
        language: str | None = None,
        context: str | None = None,
    ) -> None: ...

    def push_pcm(self, session_id: str, pcm: bytes) -> SessionPartialEvent | None: ...

    def finish_stream(self, session_id: str) -> SessionFinalEvent: ...

    def cancel_stream(self, session_id: str) -> None: ...

    def has_stream(self, session_id: str) -> bool: ...

    def drop_all_streams(self) -> None: ...


def pcm_s16le_to_float32(pcm: bytes):
    if len(pcm) % 2 != 0:
        raise LogicError(ErrorCode.invalid_request, "音频编码必须是 s16le PCM，长度为偶数")
    import numpy as np

    if not pcm:
        return np.zeros((0,), dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _default_vllm_available() -> bool:
    if importlib.util.find_spec("vllm") is None:
        return False
    try:
        import vllm  # noqa: F401
    except Exception:
        return False
    return True


def _default_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_backend(
    requested: str = DEFAULT_BACKEND,
    *,
    vllm_available_fn: Callable[[], bool] = _default_vllm_available,
    cuda_available_fn: Callable[[], bool] = _default_cuda_available,
) -> str:
    """解析推理后端：auto 在 vLLM 可导入且有 CUDA 时用 vllm，否则回退跨平台 transformers。"""
    key = (requested or DEFAULT_BACKEND).strip().lower()
    if key not in SUPPORTED_BACKENDS:
        raise LogicError(
            ErrorCode.invalid_request,
            f"不支持的 ASR 后端: {requested!r}（仅允许 {', '.join(SUPPORTED_BACKENDS)}）",
        )
    if key == "vllm":
        return "vllm"
    if key == "transformers":
        return "transformers"
    if cuda_available_fn() and vllm_available_fn():
        return "vllm"
    return "transformers"


def resolve_transformers_device(
    explicit_device: str | None = None,
    *,
    torch_module: Any = None,
) -> tuple[str, Any]:
    """为 Transformers 后端选择设备（cuda / mps / cpu）与匹配的浮点精度。"""
    torch = torch_module
    if torch is None:
        import torch as _torch

        torch = _torch

    if explicit_device and explicit_device.strip():
        dev = explicit_device.strip().lower()
    elif bool(torch.cuda.is_available()):
        dev = "cuda"
    elif bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
        dev = "mps"
    else:
        dev = "cpu"

    if dev.startswith("cuda"):
        bf16_ok = bool(
            callable(getattr(torch.cuda, "is_bf16_supported", None))
            and torch.cuda.is_bf16_supported()
        )
        dtype = torch.bfloat16 if bf16_ok else torch.float16
    elif dev == "mps":
        dtype = torch.float16
    else:
        dtype = torch.float32
    return dev, dtype


def unify_asr_tokenizer(asr_obj: Any) -> None:
    """统一 vLLM 内部 tokenizer 与 processor.tokenizer，消除 fix_mistral_regex 错位。"""
    if getattr(asr_obj, "backend", None) != "vllm":
        return
    model_obj = getattr(asr_obj, "model", None)
    get_tok = getattr(model_obj, "get_tokenizer", None)
    if not callable(get_tok):
        return
    vllm_tok = get_tok()
    if vllm_tok is None or not hasattr(asr_obj, "processor"):
        return
    old_tok = getattr(asr_obj.processor, "tokenizer", None)
    if old_tok is not None:
        for attr in ("audio_token", "audio_bos_token", "audio_eos_token", "chat_template"):
            if not hasattr(vllm_tok, attr) and hasattr(old_tok, attr):
                try:
                    setattr(vllm_tok, attr, getattr(old_tok, attr))
                except Exception:
                    pass
    asr_obj.processor.tokenizer = vllm_tok


@dataclass
class _Stream:
    session_id: str
    decoder: BoundedOfficialStream | None
    samples: int = 0
    last_text: str = ""
    language: str | None = None


class OfficialEngine:
    """单模型官方引擎，同时支持 vLLM（Linux CUDA）与 Transformers（CUDA / MPS / CPU）。"""

    def __init__(
        self,
        *,
        model_path: Path,  # 由看护层按 AppPaths.asr_model_dir 显式传入，无默认位置
        backend: str = DEFAULT_BACKEND,
        device: str | None = None,
        gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION,
        max_model_len: int = DEFAULT_MAX_MODEL_LEN,
        max_num_seqs: int = DEFAULT_VLLM_MAX_NUM_SEQS,
        chunk_size_sec: float = DEFAULT_CHUNK_SIZE_SEC,
        unfixed_chunk_num: int = DEFAULT_UNFIXED_CHUNK_NUM,
        unfixed_token_num: int = DEFAULT_UNFIXED_TOKEN_NUM,
        max_session_samples: int = DEFAULT_MAX_SESSION_SAMPLES,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        if backend not in SUPPORTED_BACKENDS:
            raise LogicError(
                ErrorCode.invalid_request,
                f"不支持的 ASR 后端: {backend!r}（仅允许 {', '.join(SUPPORTED_BACKENDS)}）",
            )
        self.model_path = Path(model_path)
        self.backend = backend
        self.device = device
        self.resolved_backend: str | None = None
        self.resolved_device: str | None = None
        if isinstance(max_num_seqs, bool) or int(max_num_seqs) <= 0:
            raise LogicError(
                ErrorCode.invalid_request,
                f"max_num_seqs 必须为正整数，收到: {max_num_seqs!r}",
            )
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len
        self.max_num_seqs = int(max_num_seqs)
        self.chunk_size_sec = chunk_size_sec
        self.unfixed_chunk_num = unfixed_chunk_num
        self.unfixed_token_num = unfixed_token_num
        self.max_session_samples = max_session_samples
        self.sample_rate = sample_rate
        self.loaded = False
        self.load_s: float | None = None
        self.warmup_s: float | None = None
        self.partial_policy = OFFICIAL_PARTIAL_POLICY
        self.blockers = ASR_STREAMING_BLOCKERS
        self._asr: Any = None
        self._guard = threading.RLock()
        self._streams: dict[str, _Stream] = {}
        self.last_finished_reports: list[StepReport] = []
        self.last_transcribe_reports: list[StepReport] = []

    def load(self, *, warmup: bool = True) -> None:
        if self.loaded:
            return
        if not self.model_path.is_dir():
            raise LogicError(
                ErrorCode.asr_unavailable,
                f"官方模型目录不存在: {self.model_path}",
            )
        chosen_backend = resolve_backend(self.backend)
        started = time.perf_counter()
        from qwen_asr import Qwen3ASRModel

        if chosen_backend == "vllm":
            # 根因约束：Qwen3ASRProcessingInfo 声明 audio 上限为 None，若不约束 max_num_seqs /
            # max_num_batched_tokens / limit_mm_per_prompt，vLLM 0.14 会按默认 8192 token 预算
            # 同时 profile 21 段最大音频，在 0.45 显存配额下直接 KV Cache 负值 OOM。
            # 本 sidecar 单流串行，每请求仅 1 段音频，收口后仅 profile 2 段且显存余量充足。
            self._asr = Qwen3ASRModel.LLM(
                model=str(self.model_path),
                gpu_memory_utilization=self.gpu_memory_utilization,
                max_model_len=self.max_model_len,
                max_num_seqs=self.max_num_seqs,
                max_num_batched_tokens=self.max_model_len,
                limit_mm_per_prompt={"audio": 1},
                disable_log_stats=True,
            )
            unify_asr_tokenizer(self._asr)
            self.resolved_backend = "vllm"
            self.resolved_device = "cuda"
        else:
            import torch

            dev, dtype = resolve_transformers_device(self.device, torch_module=torch)
            try:
                self._asr = Qwen3ASRModel.from_pretrained(
                    str(self.model_path),
                    dtype=dtype,
                )
                self._asr.model.to(dev)
                if callable(getattr(self._asr.model, "eval", None)):
                    self._asr.model.eval()
                self._asr.device = torch.device(dev)
                self._asr.dtype = dtype
                self.resolved_backend = "transformers"
                self.resolved_device = dev
            except BaseException as exc:
                self._asr = None
                self.loaded = False
                self.resolved_backend = None
                self.resolved_device = None
                if is_cuda_reclaim_target("transformers", dev):
                    _clear_exception_frames(exc)
                    gc.collect()
                    with suppress(Exception):
                        reclaim_cuda_cache(
                            backend="transformers",
                            device=dev,
                            force=True,
                            torch_module=torch,
                        )
                raise

        self.loaded = True
        self.load_s = time.perf_counter() - started
        if warmup:
            warm_started = time.perf_counter()
            silence = b"\x00\x00" * int(self.sample_rate * MIN_INPUT_S)
            self.transcribe(silence, language="zh")
            self.warmup_s = time.perf_counter() - warm_started
        else:
            self._reclaim_cuda_cache(force=True)

    def _cuda_cache_target(self) -> tuple[str | None, Any]:
        if self._asr is None:
            return None, None
        backend = getattr(self._asr, "backend", None) or self.resolved_backend
        device = (
            getattr(self._asr, "device", None)
            or getattr(getattr(self._asr, "model", None), "device", None)
            or self.resolved_device
        )
        return backend, device

    def _reclaim_cuda_cache(
        self, *, force: bool = True, suppress_errors: bool = False
    ) -> bool:
        backend, device = self._cuda_cache_target()
        if suppress_errors:
            with suppress(Exception):
                return reclaim_cuda_cache(backend=backend, device=device, force=force)
            return False
        return reclaim_cuda_cache(backend=backend, device=device, force=force)

    def _clear_streams_unlocked(self) -> None:
        for stream in self._streams.values():
            stream.decoder = None
        self._streams.clear()

    def _require(self) -> Any:
        if self._asr is None or not self.loaded:
            raise LogicError(ErrorCode.asr_unavailable, "官方引擎尚未 load，拒绝假装可用")
        return self._asr

    def _init_streaming_state_for_backend(
        self,
        asr_obj: Any,
        *,
        context: str,
        language: str | None,
        normalize_language_name: Callable[[str], str],
    ) -> Any:
        backend_kind = str(getattr(asr_obj, "backend", "vllm") or "vllm")
        if backend_kind != "transformers":
            try:
                return asr_obj.init_streaming_state(
                    context=context,
                    language=language,
                    unfixed_chunk_num=self.unfixed_chunk_num,
                    unfixed_token_num=self.unfixed_token_num,
                    chunk_size_sec=self.chunk_size_sec,
                )
            except ValueError as exc:
                raise LogicError(
                    ErrorCode.invalid_request, f"识别会话参数不合法: {exc}"
                ) from exc

        # Transformers 后端：复用同一 ASRStreamingState 数据结构与 _build_text_prompt
        import numpy as np
        from qwen_asr.inference.qwen3_asr import ASRStreamingState
        from qwen_asr.inference.utils import validate_language

        if self.chunk_size_sec <= 0:
            raise LogicError(ErrorCode.invalid_request, "chunk_size_sec 必须大于 0")
        force_language: str | None = None
        if language is not None and str(language).strip():
            try:
                canonical = normalize_language_name(str(language))
                validate_language(canonical)
            except ValueError as exc:
                raise LogicError(
                    ErrorCode.invalid_request, f"不支持的识别语言 {language!r}: {exc}"
                ) from exc
            force_language = canonical

        chunk_samples = max(1, int(round(float(self.chunk_size_sec) * self.sample_rate)))
        prompt_raw = asr_obj._build_text_prompt(
            context=context, force_language=force_language
        )
        return ASRStreamingState(
            unfixed_chunk_num=int(self.unfixed_chunk_num),
            unfixed_token_num=int(self.unfixed_token_num),
            chunk_size_sec=float(self.chunk_size_sec),
            chunk_size_samples=chunk_samples,
            chunk_id=0,
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            prompt_raw=prompt_raw,
            context=context,
            force_language=force_language,
            language="",
            text="",
            _raw_decoded="",
        )

    def _new_decoder(
        self, *, language: str | None, context: str | None
    ) -> BoundedOfficialStream:
        from qwen_asr.inference.utils import normalize_language_name

        asr_obj = self._require()
        norm_lang = normalize_language(language)
        state = self._init_streaming_state_for_backend(
            asr_obj,
            context=context or "",
            language=norm_lang,
            normalize_language_name=normalize_language_name,
        )
        backend_kind = str(getattr(asr_obj, "backend", "vllm") or "vllm")
        params_factory = (
            None if backend_kind == "transformers" else self._step_sampling_params
        )
        return BoundedOfficialStream(
            asr_obj,
            state,
            sampling_params_factory=params_factory,
            parse_output=lambda raw, lang: parse_asr_text(
                raw, lang, normalize_language_name=normalize_language_name
            ),
            pcm=numpy_pcm_ops(),
        )

    def transcribe(
        self, pcm: bytes, *, language: str | None = None, context: str | None = None
    ) -> SttResponse:
        self._require()
        samples = len(pcm) // 2
        if samples > self.max_session_samples:
            raise LogicError(
                ErrorCode.invalid_request,
                "整段音频超过 sidecar 单次识别上限",
                details={
                    "max_session_samples": self.max_session_samples,
                    "samples": samples,
                },
            )
        decoder = self._new_decoder(language=language, context=context)
        failed = False
        try:
            decode_whole(
                decoder,
                pcm_s16le_to_float32(pcm),
                min_samples=int(self.sample_rate * MIN_INPUT_S),
            )
        except BaseException:
            failed = True
            raise
        finally:
            self.last_transcribe_reports = list(decoder.reports)
            self._reclaim_cuda_cache(force=True, suppress_errors=failed)
        state = decoder.state
        return SttResponse(
            text=getattr(state, "text", "") or "",
            language=getattr(state, "language", None) or None,
        )

    def start_stream(
        self,
        session_id: str,
        *,
        language: str | None = None,
        context: str | None = None,
    ) -> None:
        self._require()
        with self._guard:
            if session_id in self._streams:
                raise LogicError(ErrorCode.conflict, "会话已存在")
            decoder = self._new_decoder(language=language, context=context)
            self._streams[session_id] = _Stream(session_id=session_id, decoder=decoder)

    @staticmethod
    def _step_sampling_params(max_tokens: int):
        from vllm import SamplingParams

        return SamplingParams(temperature=0.0, max_tokens=int(max_tokens))

    def stream_reports(self, session_id: str) -> list[StepReport]:
        with self._guard:
            stream = self._streams.get(session_id)
            if stream is None or stream.decoder is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            return list(stream.decoder.reports)

    def push_pcm(self, session_id: str, pcm: bytes) -> SessionPartialEvent | None:
        self._require()
        with self._guard:
            stream = self._streams.get(session_id)
            if stream is None or stream.decoder is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            incoming = len(pcm) // 2
            if stream.samples + incoming > self.max_session_samples:
                raise LogicError(
                    ErrorCode.invalid_request,
                    "会话音频超过 sidecar 缓存上限，拒绝继续接收；长音频达标仍阻塞，不自动拼接",
                    details={
                        "max_session_samples": self.max_session_samples,
                        "samples": stream.samples + incoming,
                    },
                )
            stream.samples += incoming
            wav = pcm_s16le_to_float32(pcm)
            stream.decoder.feed(wav)
            state = stream.decoder.state
            text = getattr(state, "text", "") or ""
            language = getattr(state, "language", None) or None
            if text == stream.last_text and language == stream.language:
                return None
            stream.last_text = text
            stream.language = language
            audio_end_s = stream.samples / float(self.sample_rate)
            return SessionPartialEvent(
                session_id=session_id,
                stable_text="",
                tail_text=text,
                language=language,
                audio_end_s=audio_end_s,
            )

    def finish_stream(self, session_id: str) -> SessionFinalEvent:
        self._require()
        with self._guard:
            stream = self._streams.pop(session_id, None)
            if stream is None or stream.decoder is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            decoder = stream.decoder
            stream.decoder = None
            failed = False
            try:
                decoder.finish()
            except BaseException:
                failed = True
                raise
            finally:
                self.last_finished_reports = list(decoder.reports)
                self._reclaim_cuda_cache(force=True, suppress_errors=failed)
            state = decoder.state
            text = getattr(state, "text", "") or ""
            language = getattr(state, "language", None) or None
            return SessionFinalEvent(session_id=session_id, text=text, language=language)

    def cancel_stream(self, session_id: str) -> None:
        with self._guard:
            stream = self._streams.pop(session_id, None)
            if stream is None:
                raise LogicError(ErrorCode.not_found, f"会话不存在: {session_id}")
            stream.decoder = None
            self._reclaim_cuda_cache(force=True)

    def has_stream(self, session_id: str) -> bool:
        with self._guard:
            return session_id in self._streams

    def drop_all_streams(self) -> None:
        with self._guard:
            self._clear_streams_unlocked()
            self._reclaim_cuda_cache(force=True)

    def unload(self) -> None:
        with self._guard:
            backend, device = self._cuda_cache_target()
            self._clear_streams_unlocked()
            self._asr = None
            self.loaded = False
            self.resolved_backend = None
            self.resolved_device = None
        if is_cuda_reclaim_target(backend, device):
            gc.collect()
            reclaim_cuda_cache(backend=backend, device=device, force=True)


__all__ = [
    "AsrEngine",
    "MIN_INPUT_S",
    "OfficialEngine",
    "pcm_s16le_to_float32",
    "resolve_backend",
    "resolve_transformers_device",
    "unify_asr_tokenizer",
]
