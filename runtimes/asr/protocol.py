"""Sidecar 与 `_asr.AsrSidecarClient` 共用的路径、默认参数和语言别名规范化。

官方 `ASRStreamingState` 只公开 `text` / `language`（另有内部 `_raw_decoded`）。
`unfixed_token_num` 只用于官方 Prompt 前缀回退，不是对外稳定前沿字段。
因此流式 partial 把当前官方 `text` 全部放在 `tail_text`，`stable_text` 保持空，
直到有界内核 `stream_step.BoundedOfficialStream.finish` 给出定稿；不得自行按最后 K 个 token 切 stable。
"""
from __future__ import annotations

from _contracts import (
    ASR_STREAMING_BLOCKERS,
    SIDECAR_CHUNK_HEADER,
    SIDECAR_DEFAULT_BASE_URL,
    SIDECAR_SESSIONS_PATH,
    SIDECAR_TRANSCRIBE_PATH,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_GPU_MEMORY_UTILIZATION = 0.45
DEFAULT_MAX_MODEL_LEN = 4096
DEFAULT_VLLM_MAX_NUM_SEQS = 2
DEFAULT_BACKEND = "auto"
SUPPORTED_BACKENDS = ("auto", "vllm", "transformers")

DEFAULT_CHUNK_SIZE_SEC = 0.4
DEFAULT_UNFIXED_CHUNK_NUM = 5
DEFAULT_UNFIXED_TOKEN_NUM = 5
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_MAX_SESSION_SAMPLES = DEFAULT_SAMPLE_RATE * 120
DEFAULT_IDLE_S = 180.0
DEFAULT_SWEEP_INTERVAL_S = 15.0

OFFICIAL_PARTIAL_POLICY = (
    "官方 ASRStreamingState 无独立 stable 输出，只更新 state.text / state.language。"
    "unfixed_token_num 仅用于内部 prefix 回退，不是可引用的稳定前沿。"
    "本 sidecar 的 SessionPartialEvent.stable_text 保持空字符串，tail_text 为当前官方 text；"
    "定稿只来自 BoundedOfficialStream.finish（官方 finish_streaming_transcribe 的有界实现）。不得用字符串切片冒充稳定。"
)

LANGUAGE_ALIASES: dict[str, str | None] = {
    "auto": None,
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "zh_cn": "Chinese",
    "zh-hans": "Chinese",
    "zh-tw": "Chinese",
    "zh_tw": "Chinese",
    "zh-hant": "Chinese",
    "chi": "Chinese",
    "chi_sim": "Chinese",
    "zho": "Chinese",
    "chinese": "Chinese",
    "en": "English",
    "en-us": "English",
    "en_us": "English",
    "en-gb": "English",
    "en_gb": "English",
    "eng": "English",
    "english": "English",
    "ja": "Japanese",
    "ja-jp": "Japanese",
    "ja_jp": "Japanese",
    "jp": "Japanese",
    "jpn": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "ko-kr": "Korean",
    "ko_kr": "Korean",
    "kor": "Korean",
    "korean": "Korean",
    "yue": "Cantonese",
    "yue-hk": "Cantonese",
    "zh-yue": "Cantonese",
    "cantonese": "Cantonese",
}


def normalize_language(value: str | None) -> str | None:
    """规范化识别语言参数；`auto` 或空白归一为 `None`（交由模型自动语种识别）。"""
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    lower = key.lower()
    if lower in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[lower]
    return key


__all__ = [
    "ASR_STREAMING_BLOCKERS",
    "DEFAULT_BACKEND",
    "DEFAULT_CHUNK_SIZE_SEC",
    "DEFAULT_GPU_MEMORY_UTILIZATION",
    "DEFAULT_HOST",
    "DEFAULT_IDLE_S",
    "DEFAULT_MAX_MODEL_LEN",
    "DEFAULT_MAX_SESSION_SAMPLES",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_SWEEP_INTERVAL_S",
    "DEFAULT_UNFIXED_CHUNK_NUM",
    "DEFAULT_UNFIXED_TOKEN_NUM",
    "DEFAULT_VLLM_MAX_NUM_SEQS",
    "LANGUAGE_ALIASES",
    "OFFICIAL_PARTIAL_POLICY",
    "SIDECAR_CHUNK_HEADER",
    "SIDECAR_DEFAULT_BASE_URL",
    "SIDECAR_SESSIONS_PATH",
    "SIDECAR_TRANSCRIBE_PATH",
    "SUPPORTED_BACKENDS",
    "normalize_language",
]
