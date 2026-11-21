"""把桌面业务流程挂到五键快捷键动作接口（语音 / 截屏原位翻译 / 纯 OCR / 重复输入 / 截屏标注）。"""
from __future__ import annotations

import threading
from typing import Any

from _desktop.delivery import RepeatAction
from _desktop.voice import VoiceFlow


class DesktopActions:
    def __init__(
        self,
        *,
        voice: VoiceFlow,
        capture: Any,
        repeat: RepeatAction,
        spawn=None,
    ) -> None:
        self.voice = voice
        self.capture = capture
        self.repeat_action = repeat
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def voice_start(self) -> bool:
        return self.voice.start()

    def voice_stop(self) -> None:
        self.voice.stop()

    def screenshot(self) -> None:
        self.capture.trigger_translate()

    def ocr(self) -> None:
        self.capture.trigger_ocr()

    def repeat(self) -> None:
        self._spawn(self.repeat_action.run)

    def annotate(self) -> None:
        self.capture.trigger_annotate()

    def overlay_active(self) -> bool:
        return self.capture.overlay_active()

    def close_overlay(self) -> bool:
        return self.capture.handle_escape()
