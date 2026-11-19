"""ASR sidecar 宿主。本包在 import 时不得加载 torch / vLLM / 模型。

官方引擎（vLLM 或跨平台 Transformers）只在显式 `serve --load` 或 `OfficialEngine.load()` 时进入本进程。
协议与应用侧 `_asr.AsrSidecarClient` / `_contracts` 路径一致，不另起一套旁支。
"""

from runtimes.asr.protocol import (
    DEFAULT_BACKEND,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_MAX_MODEL_LEN,
    OFFICIAL_PARTIAL_POLICY,
    SUPPORTED_BACKENDS,
)

__all__ = [
    "DEFAULT_BACKEND",
    "DEFAULT_GPU_MEMORY_UTILIZATION",
    "DEFAULT_MAX_MODEL_LEN",
    "OFFICIAL_PARTIAL_POLICY",
    "SUPPORTED_BACKENDS",
]
