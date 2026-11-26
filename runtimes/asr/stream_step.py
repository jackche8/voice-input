"""官方流式解码一步的受控双后端（vLLM + Transformers）实现。

核心约束：
1. 每步 max_tokens 取当前音频跨度的语速上界，拒绝失控复读步（finish_reason == "length" 不写入历史）；
2. 同时支持 Linux `vllm` 与跨平台 `transformers`（CUDA / MPS / CPU）流式逐步解码；
3. 统一底层与前缀回退的分词器实例，彻底消除 `fix_mistral_regex` 错位；
4. `_finish_prefix` 剥离尾部残缺多字节字符产生的 `\ufffd`；
5. `finish()` 对 `< 8000` 采样点（0.5s）的非空音频自动尾部补静音至 0.5s 再定稿。
"""
from __future__ import annotations

import gc
import math
import sys
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from runtimes.asr.protocol import DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES

SAMPLE_RATE = 16000
MIN_FINISH_SAMPLES = SAMPLE_RATE // 2  # 0.5s @ 16kHz = 8000 samples
MAX_TOKENS_PER_AUDIO_S = 12.0
LAG_ALLOWANCE_S = 2.0
SLACK_TOKENS = 16
DEFAULT_EOS_TOKEN_IDS = frozenset({151643, 151645})

ParseOutput = Callable[[str, "str | None"], "tuple[str, str]"]


@dataclass(frozen=True)
class PcmOps:
    """一维 float PCM 的运算；生产实现见 numpy_pcm_ops()。切片用对象自身的 [a:b]。"""

    zeros: Callable[[int], Any]
    concat: Callable[[Any, Any], Any]
    size: Callable[[Any], int]


def numpy_pcm_ops() -> PcmOps:
    import numpy as np

    return PcmOps(
        zeros=lambda n: np.zeros((int(n),), dtype=np.float32),
        concat=lambda a, b: b if a.shape[0] == 0 else np.concatenate([a, b], axis=0),
        size=lambda a: int(a.shape[0]),
    )


@dataclass(frozen=True)
class StepReport:
    """一次模型调用的事实记录，供证据与测试使用，不进入产品协议。"""

    audio_s: float
    prefix_tokens: int
    rollback_tokens: int
    max_tokens: int
    gen_tokens: int
    prompt_tokens: int
    finish_reason: str
    accepted: bool
    final: bool
    dt_s: float


def _collect_eos_ids(raw_eos: Any, target: set[int]) -> None:
    if isinstance(raw_eos, int):
        target.add(raw_eos)
    elif isinstance(raw_eos, (list, tuple, set, frozenset)):
        for item in raw_eos:
            if isinstance(item, int):
                target.add(item)


def _normalize_cuda_device(device: Any) -> Any | None:
    """规范化 CUDA 设备标识；非 CUDA 或畸形字符串返回 None，绝不触发 `import torch`。"""
    if device is None:
        return None
    if isinstance(device, str):
        text = device.strip().lower()
        if text == "cuda":
            return "cuda"
        if text.startswith("cuda:"):
            idx = text[5:]
            if idx.isascii() and idx.isdigit():
                return text
        return None
    dev_type = getattr(device, "type", None)
    if isinstance(dev_type, str) and dev_type.strip().lower() == "cuda":
        return device
    return None


def is_cuda_reclaim_target(backend: str | None, device: Any) -> bool:
    """仅 `transformers + cuda` 需要主动回收 PyTorch CUDA 缓存段（vLLM / CPU / MPS 返回 False）。"""
    return (backend or "").strip().lower() == "transformers" and _normalize_cuda_device(device) is not None


def _clear_exception_frames(exc: BaseException) -> None:
    """清空异常链中已退出内层栈帧的局部变量（避免 GPU 张量被 traceback 钉住），保留行号、类型与消息。"""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        obj_id = id(cur)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if cur.__traceback__ is not None:
            traceback.clear_frames(cur.__traceback__)
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None:
            stack.append(cur.__context__)
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)


def reclaim_cuda_cache(
    *,
    backend: str | None,
    device: Any,
    force: bool = False,
    threshold_bytes: int = DEFAULT_CUDA_CACHE_FLUSH_THRESHOLD_BYTES,
    torch_module: Any = None,
) -> bool:
    """仅对 Transformers + CUDA 回收 PyTorch CUDA caching allocator 冗余缓存段，返回本次是否执行了 empty_cache()。

    从不执行 `import torch`（若 `sys.modules` 未加载 `torch` 则本进程物理上不可能持有 PyTorch CUDA 缓存）。
    """
    if (backend or "").strip().lower() != "transformers":
        return False
    query_device = _normalize_cuda_device(device)
    if query_device is None:
        return False
    torch = torch_module if torch_module is not None else sys.modules.get("torch")
    if torch is None:
        return False
    cuda = getattr(torch, "cuda", None)
    if cuda is None or not callable(getattr(cuda, "is_available", None)) or not cuda.is_available():
        return False
    if not force:
        reserved = int(cuda.memory_reserved(query_device))
        allocated = int(cuda.memory_allocated(query_device))
        if reserved - allocated < threshold_bytes:
            return False
    cuda.empty_cache()
    return True


class BoundedOfficialStream:
    """包装一个官方 ASRStreamingState；支持 vLLM 与 Transformers 双后端，同一会话须串行调用。"""

    def __init__(
        self,
        asr: Any,
        state: Any,
        *,
        sampling_params_factory: Callable[[int], Any] | None = None,
        parse_output: ParseOutput,
        pcm: PcmOps,
    ) -> None:
        self._asr = asr
        self.state = state
        self._make_params = sampling_params_factory
        self._parse = parse_output
        self._pcm = pcm
        self.rejected_streak = 0
        self.stalled_chunks = 0
        self.reports: list[StepReport] = []

    @property
    def audio_s(self) -> float:
        return self._pcm.size(self.state.audio_accum) / float(SAMPLE_RATE)

    def feed(self, wav: Any) -> bool:
        """追加 float PCM；每凑满一个 chunk 解码一步。返回本次是否至少接受了一步。"""
        state, pcm = self.state, self._pcm
        if pcm.size(wav) > 0:
            state.buffer = pcm.concat(state.buffer, wav)
        accepted_any = False
        while pcm.size(state.buffer) >= state.chunk_size_samples:
            chunk = state.buffer[: state.chunk_size_samples]
            state.buffer = state.buffer[state.chunk_size_samples :]
            state.audio_accum = pcm.concat(state.audio_accum, chunk)
            prefix, rollback = self._step_prefix()
            accepted_any |= self._decode(prefix, rollback, final=False)
        return accepted_any

    def finish(self) -> None:
        """冲刷不足一个 chunk 的尾音频并定稿。

        - 总音频为空（0 采样点）直接返回；
        - 总音频非空但不足 8000 采样点（0.5s）时，尾部补零静音至 8000 采样点再执行定稿解码；
        - 若存在尾音频、短音频补零或此前有被拒步未覆盖音频，执行受语速上界保护的定稿步。
        """
        state, pcm = self.state, self._pcm
        tail_n = pcm.size(state.buffer) if state.buffer is not None else 0
        accum_n = pcm.size(state.audio_accum) if state.audio_accum is not None else 0
        if accum_n + tail_n == 0:
            return
        if tail_n > 0:
            state.audio_accum = pcm.concat(state.audio_accum, state.buffer)
            state.buffer = pcm.zeros(0)

        padded_n = 0
        cur_total = pcm.size(state.audio_accum)
        if 0 < cur_total < MIN_FINISH_SAMPLES:
            padded_n = MIN_FINISH_SAMPLES - cur_total
            state.audio_accum = pcm.concat(state.audio_accum, pcm.zeros(padded_n))

        if tail_n == 0 and padded_n == 0 and self.rejected_streak == 0:
            return

        prefix, rollback = self._finish_prefix()
        pending_s = (
            (tail_n + padded_n) / float(SAMPLE_RATE)
            + state.chunk_size_sec * self.stalled_chunks
        )
        if not self._decode(prefix, rollback, final=True, pending_s=pending_s):
            from _contracts import ErrorCode, LogicError

            raise LogicError(
                ErrorCode.asr_unavailable,
                "定稿解码超过语速上界（模型复读失控），拒绝把截断结果当定稿",
                details={
                    "audio_s": round(self.audio_s, 3),
                    "max_tokens": self.reports[-1].max_tokens,
                },
            )

    def _backend(self) -> str:
        return str(getattr(self._asr, "backend", "vllm") or "vllm")

    def _transformers_device(self) -> Any:
        return getattr(self._asr, "device", None) or getattr(
            getattr(self._asr, "model", None), "device", None
        )

    def _tokenizer(self) -> Any:
        # vLLM 后端优先直接取 vLLM 引擎内部同一分词器实例，彻底消除 fix_mistral_regex 错位
        if self._backend() == "vllm":
            model_obj = getattr(self._asr, "model", None)
            get_tok = getattr(model_obj, "get_tokenizer", None)
            if callable(get_tok):
                tok = get_tok()
                if tok is not None:
                    return tok
        return self._asr.processor.tokenizer

    def _eos_token_ids(self) -> set[int]:
        eos_ids = set(DEFAULT_EOS_TOKEN_IDS)
        model_obj = getattr(self._asr, "model", None)
        gen_cfg = getattr(model_obj, "generation_config", None)
        if gen_cfg is not None:
            _collect_eos_ids(getattr(gen_cfg, "eos_token_id", None), eos_ids)
        tok = self._tokenizer()
        _collect_eos_ids(getattr(tok, "eos_token_id", None), eos_ids)
        return eos_ids

    def _step_prefix(self) -> tuple[str, int]:
        state = self.state
        if state.chunk_id < state.unfixed_chunk_num:
            return "", 0
        tok = self._tokenizer()
        cur_ids = tok.encode(state._raw_decoded)
        k = int(state.unfixed_token_num)
        while True:
            end_idx = max(0, len(cur_ids) - k)
            prefix = tok.decode(cur_ids[:end_idx]) if end_idx > 0 else ""
            if "\ufffd" not in prefix:
                return prefix, len(cur_ids) - end_idx
            if end_idx == 0:
                return "", len(cur_ids)
            k += 1

    def _finish_prefix(self) -> tuple[str, int]:
        state = self.state
        if state.chunk_id < state.unfixed_chunk_num:
            return "", 0
        tok = self._tokenizer()
        cur_ids = tok.encode(state._raw_decoded)
        if not cur_ids:
            return "", 0
        end_idx = max(1, len(cur_ids) - int(state.unfixed_token_num))
        raw = tok.decode(cur_ids[:end_idx])
        had_replacement = False
        while raw.endswith("\ufffd"):
            had_replacement = True
            raw = raw[:-1]
        if had_replacement:
            kept_tokens = len(tok.encode(raw)) if raw else 0
            return raw, max(len(cur_ids) - end_idx, len(cur_ids) - kept_tokens)
        return raw, len(cur_ids) - end_idx

    def max_tokens_for(
        self, prefix: str, rollback: int, *, pending_s: float | None = None
    ) -> int:
        if not prefix:
            uncovered_s = self.audio_s
        elif pending_s is None:
            uncovered_s = self.state.chunk_size_sec + LAG_ALLOWANCE_S
        else:
            uncovered_s = pending_s + LAG_ALLOWANCE_S
        return rollback + SLACK_TOKENS + int(math.ceil(MAX_TOKENS_PER_AUDIO_S * uncovered_s))

    def _generate_vllm(
        self, prompt: str, audio_accum: Any, max_tokens: int
    ) -> tuple[str, int, int, str, float]:
        if self._make_params is None:
            raise RuntimeError("vLLM 后端必须提供 sampling_params_factory")
        inp = {"prompt": prompt, "multi_modal_data": {"audio": [audio_accum]}}
        started = time.perf_counter()
        outputs = self._asr.model.generate(
            [inp], sampling_params=self._make_params(max_tokens), use_tqdm=False
        )
        dt = time.perf_counter() - started
        out = outputs[0].outputs[0]
        finish_reason = str(out.finish_reason)
        gen_tokens = len(out.token_ids)
        prompt_tokens = len(outputs[0].prompt_token_ids or ())
        return out.text, gen_tokens, prompt_tokens, finish_reason, dt

    def _generate_transformers(
        self, prompt: str, audio_accum: Any, max_tokens: int
    ) -> tuple[str, int, int, str, float]:
        started = time.perf_counter()
        inputs = self._asr.processor(
            text=[prompt],
            audio=[audio_accum],
            return_tensors="pt",
            padding=True,
        )
        device = self._transformers_device()
        dtype = getattr(self._asr, "dtype", None) or getattr(
            self._asr.model, "dtype", None
        )
        if device is not None and hasattr(inputs, "to"):
            inputs = inputs.to(device)
        if dtype is not None and hasattr(inputs, "to"):
            inputs = inputs.to(dtype)

        gen_result = self._asr.model.generate(
            **inputs,
            max_new_tokens=int(max_tokens),
            do_sample=False,
        )
        dt = time.perf_counter() - started

        seqs = getattr(gen_result, "sequences", gen_result)
        input_ids = inputs["input_ids"]
        prompt_tokens = int(input_ids.shape[1]) if hasattr(input_ids, "shape") else len(input_ids[0])
        gen_slice = seqs[:, prompt_tokens:] if hasattr(seqs, "shape") else [row[prompt_tokens:] for row in seqs]
        first_seq = gen_slice[0]
        gen_ids = (
            [int(x) for x in first_seq.tolist()]
            if hasattr(first_seq, "tolist")
            else [int(x) for x in first_seq]
        )
        gen_tokens = len(gen_ids)
        eos_ids = self._eos_token_ids()
        if gen_tokens >= max_tokens and (not gen_ids or gen_ids[-1] not in eos_ids):
            finish_reason = "length"
        else:
            finish_reason = "stop"

        decoded_batch = self._asr.processor.batch_decode(
            gen_slice,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        out_text = decoded_batch[0] if decoded_batch else ""
        return out_text, gen_tokens, prompt_tokens, finish_reason, dt

    def _decode(
        self,
        prefix: str,
        rollback: int,
        *,
        final: bool,
        pending_s: float | None = None,
    ) -> bool:
        state = self.state
        max_tokens = self.max_tokens_for(prefix, rollback, pending_s=pending_s)
        prompt = state.prompt_raw + prefix
        if self._backend() == "transformers":
            device = self._transformers_device()
            try:
                out_text, gen_tokens, prompt_tokens, finish_reason, dt = (
                    self._generate_transformers(prompt, state.audio_accum, max_tokens)
                )
            except BaseException as exc:
                if is_cuda_reclaim_target("transformers", device):
                    _clear_exception_frames(exc)
                    gc.collect()
                    with suppress(Exception):
                        reclaim_cuda_cache(
                            backend="transformers",
                            device=device,
                            force=True,
                        )
                raise
            reclaim_cuda_cache(
                backend="transformers",
                device=device,
                force=False,
            )
        else:
            out_text, gen_tokens, prompt_tokens, finish_reason, dt = (
                self._generate_vllm(prompt, state.audio_accum, max_tokens)
            )

        accepted = finish_reason != "length"
        self.reports.append(
            StepReport(
                audio_s=self.audio_s,
                prefix_tokens=len(self._tokenizer().encode(prefix)) if prefix else 0,
                rollback_tokens=rollback,
                max_tokens=max_tokens,
                gen_tokens=gen_tokens,
                prompt_tokens=prompt_tokens,
                finish_reason=finish_reason,
                accepted=accepted,
                final=final,
                dt_s=dt,
            )
        )
        state.chunk_id += 1
        if not accepted:
            self.rejected_streak += 1
            self.stalled_chunks += 1
            return False
        self.rejected_streak = 0
        self.stalled_chunks = 0 if gen_tokens > rollback else self.stalled_chunks + 1
        state._raw_decoded = prefix + out_text
        state.language, state.text = self._parse(state._raw_decoded, state.force_language)
        return True


def decode_whole(
    stream: BoundedOfficialStream, wav: Any, *, min_samples: int = MIN_FINISH_SAMPLES
) -> BoundedOfficialStream:
    """整段音频不配速地走同一有界流内核：短音频补零至 min_samples 后一次喂完再定稿。"""
    pcm = stream._pcm
    missing = min_samples - pcm.size(wav)
    if missing > 0:
        wav = pcm.concat(wav, pcm.zeros(missing))
    stream.feed(wav)
    stream.finish()
    return stream


__all__ = [
    "BoundedOfficialStream",
    "LAG_ALLOWANCE_S",
    "MAX_TOKENS_PER_AUDIO_S",
    "MIN_FINISH_SAMPLES",
    "PcmOps",
    "SAMPLE_RATE",
    "SLACK_TOKENS",
    "StepReport",
    "decode_whole",
    "is_cuda_reclaim_target",
    "numpy_pcm_ops",
    "reclaim_cuda_cache",
]
