"""向后兼容重导出层：真实跨平台麦克风源与提示音已统一单源沉淀至 `_platform._common`。"""
from __future__ import annotations

from _platform._common import (
    _AUDIO_LOCK,
    _BEEP_LOCK,
    SoundDeviceSource,
    play_beep,
)

# 保持历史私有锁名兼容，与 `_platform._common._AUDIO_LOCK` 指向同一把锁
_PORTAUDIO_LOCK = _AUDIO_LOCK

__all__ = [
    "SoundDeviceSource",
    "play_beep",
    "_AUDIO_LOCK",
    "_BEEP_LOCK",
    "_PORTAUDIO_LOCK",
]
