"""截屏原位翻译（Ctrl+Alt+X）与纯 OCR（Ctrl+Alt+O）统一业务编排。

两个入口共用同一条链路：透明全屏红十字框选（正在框选或截屏时忽略重复触发）→ 松手后只截选区 →
打开同一个原位浮窗（mode=translate / ocr，创建即加载态）→ 后台 `POST /api/v1/ocr-inplace`
（OCR 模式 target_lang="raw"）→ 浮窗显示结果，或显示失败原因 +「重试」。
浮窗自己显示加载、结果与失败，本流程不向语音底栏发 busy/idle；只有没有浮窗可显示的失败（框选器打不开、
截屏失败、浮窗打不开）才发 status.error。复制由浮窗菜单发起，`_handle_copy_text` 返回是否写入成功，浮窗如实显示。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from _contracts import ErrorCode, OcrTextBlock
from _desktop.api import api_error_code, api_error_reason
from _desktop.status import PlatformStatus

RectTuple = tuple[int, int, int, int]
CaptureMode = Literal["translate", "ocr"]
DEFAULT_CAPTURE_DELAY_S = 0.08


class SelectorHandle(Protocol):
    def close(self) -> Any: ...


class OverlayHandle(Protocol):
    def set_loading(self) -> None: ...

    def set_result(self, blocks: list[OcrTextBlock], full_translated_text: str, full_ocr_text: str) -> None: ...

    def set_error(self, message: str) -> None: ...  # 只传原因；浮窗按模式自带「翻译失败：/识别失败：」前缀

    def close(self) -> Any: ...  # 关闭并回调 on_closed

    def close_silent(self) -> None: ...  # 关闭不回调


OpenSelector = Callable[
    [RectTuple | None, str, Callable[[RectTuple], None], Callable[[], None]],
    SelectorHandle,
]


class OpenInplaceOverlay(Protocol):
    def __call__(
        self,
        cropped_png: bytes,
        screen_rect: RectTuple,
        *,
        mode: CaptureMode,
        virtual_rect: RectTuple | None,
        on_copy_text: Callable[[str], bool],  # 返回是否写入成功
        on_retry: Callable[[], None],
        on_closed: Callable[[], None],
    ) -> OverlayHandle: ...


def _close_handle(handle: Any) -> None:
    if handle is None:
        return
    silent = getattr(handle, "close_silent", None)
    if callable(silent):
        silent()
        return
    close = getattr(handle, "close", None)
    if callable(close):
        close()


def _parse_blocks(raw_blocks: Any) -> list[OcrTextBlock]:
    blocks: list[OcrTextBlock] = []
    for item in raw_blocks or []:
        if isinstance(item, OcrTextBlock):
            blocks.append(item)
        else:
            blocks.append(OcrTextBlock.model_validate(item))
    return blocks


class CaptureFlow:
    """截屏原位翻译与纯 OCR 的共享业务编排器（先透明框选，再仅截取选区画面，结果进同一个浮窗）。"""

    TRANSLATE_MODE_LABEL = "截屏原位翻译"
    OCR_MODE_LABEL = "纯 OCR 识别"

    def __init__(
        self,
        *,
        capture_region: Callable[[RectTuple, RectTuple | None], bytes],
        open_selector: OpenSelector,
        open_inplace_overlay: OpenInplaceOverlay,
        copy_clipboard: Callable[[str], bool],
        api: Any,
        status: PlatformStatus,
        invoke_ui: Callable[[Callable[[], None]], None],
        get_virtual_rect: Callable[[], RectTuple | None] | None = None,
        capture_delay_s: float = DEFAULT_CAPTURE_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.capture_region = capture_region
        self.open_selector = open_selector
        self.open_inplace_overlay = open_inplace_overlay
        self.copy_clipboard = copy_clipboard
        self.api = api
        self.status = status
        self.invoke_ui = invoke_ui
        self._get_virtual_rect = get_virtual_rect
        self._capture_delay_s = max(0.0, float(capture_delay_s))
        self._sleep = sleep
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, name="capture-flow", daemon=True).start())

        self._lock = threading.Lock()
        self._generation = 0
        self._selector: SelectorHandle | None = None
        self._overlay: OverlayHandle | None = None
        self._selector_opening = False
        self._overlay_opening = False
        self._capturing = False

    def trigger_translate(self) -> None:
        """Ctrl+Alt+X：框选后在原位浮窗显示译文。"""
        self._start_capture("translate", self.TRANSLATE_MODE_LABEL)

    def trigger_ocr(self) -> None:
        """Ctrl+Alt+O：框选后在同一个原位浮窗显示识别出的原文（不自动复制，由浮窗菜单复制）。"""
        self._start_capture("ocr", self.OCR_MODE_LABEL)

    def overlay_active(self) -> bool:
        with self._lock:
            return self._selector is not None or self._overlay is not None or self._selector_opening

    def close_overlay(self) -> bool:
        """Esc / 退出：关闭活跃选区或原位浮窗并失效在途后台请求。"""
        with self._lock:
            selector = self._selector
            overlay = self._overlay
            self._selector = None
            self._overlay = None
            had_active = (
                selector is not None
                or overlay is not None
                or self._selector_opening
                or self._overlay_opening
                or self._capturing
            )
            self._selector_opening = False
            self._overlay_opening = False
            self._capturing = False
            if had_active:
                self._generation += 1

        if selector is not None or overlay is not None:
            def _close_ui() -> None:
                _close_handle(selector)
                _close_handle(overlay)

            self.invoke_ui(_close_ui)
        return False

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _start_capture(self, mode: CaptureMode, mode_label: str) -> None:
        with self._lock:
            # 根治防风暴：正在打开框选器、正在拖拽或正在截取选区时直接忽略重复触发
            if self._selector is not None or self._selector_opening or self._capturing:
                return
            old_overlay = self._overlay
            self._overlay = None
            self._overlay_opening = False
            self._generation += 1
            gen = self._generation
            self._selector_opening = True

        def _open_selector_ui() -> None:
            _close_handle(old_overlay)
            if not self._is_current(gen):
                return

            vrect = self._get_virtual_rect() if self._get_virtual_rect is not None else None

            def _on_cancelled() -> None:
                with self._lock:
                    if self._generation != gen:
                        return
                    self._selector = None
                    self._selector_opening = False

            def _on_selected(rect: RectTuple) -> None:
                with self._lock:
                    if self._generation != gen:
                        return
                    self._selector = None
                    self._selector_opening = False
                    self._capturing = True
                self._spawn(lambda: self._capture_and_process(gen, mode, rect, vrect))

            try:
                selector = self.open_selector(
                    vrect,
                    mode_label,
                    _on_selected,
                    _on_cancelled,
                )
            except Exception as exc:
                msg = api_error_reason(exc)
                print(f"[capture] 框选器无法打开: {msg}", flush=True)
                with self._lock:
                    if self._generation == gen:
                        self._selector_opening = False
                if self._is_current(gen):
                    self.status.error(ErrorCode.dependency_failed, f"框选器无法打开：{msg}")
                return

            stale = False
            with self._lock:
                if self._generation != gen:
                    stale = True
                else:
                    self._selector_opening = False
                    self._selector = selector
            if stale:
                _close_handle(selector)

        self.invoke_ui(_open_selector_ui)

    def _capture_and_process(
        self,
        gen: int,
        mode: CaptureMode,
        rect: RectTuple,
        virtual_rect: RectTuple | None,
    ) -> None:
        try:
            if self._capture_delay_s > 0:
                self._sleep(self._capture_delay_s)
            if not self._is_current(gen):
                return
            png = self.capture_region(rect, virtual_rect)
        except Exception as exc:
            msg = api_error_reason(exc)
            print(f"[capture] 选区截屏失败: {msg}", flush=True)
            if self._is_current(gen):
                self.status.error(ErrorCode.dependency_failed, f"截屏失败：{msg}")
            return
        finally:
            with self._lock:
                if self._generation == gen:
                    self._capturing = False

        with self._lock:
            if self._generation != gen:
                return
            self._overlay_opening = True
        self.invoke_ui(lambda: self._open_overlay(gen, mode, png, rect, virtual_rect))

    def _open_overlay(
        self, gen: int, mode: CaptureMode, cropped_png: bytes, rect: RectTuple, virtual_rect: RectTuple | None
    ) -> None:
        if not self._is_current(gen):
            return
        overlay_box: dict[str, OverlayHandle | None] = {"handle": None}

        def _on_closed() -> None:
            with self._lock:
                if self._generation == gen and self._overlay is overlay_box["handle"]:
                    self._overlay = None
                    self._generation += 1

        def _on_retry() -> None:
            handle = overlay_box["handle"]
            if handle is None or not self._is_current(gen):
                return
            handle.set_loading()
            self._spawn(lambda: self._run_inplace(gen, mode, handle, cropped_png))

        try:
            overlay = self.open_inplace_overlay(
                cropped_png,
                rect,
                mode=mode,
                virtual_rect=virtual_rect,
                on_copy_text=self._handle_copy_text,
                on_retry=_on_retry,
                on_closed=_on_closed,
            )
        except Exception as exc:
            msg = api_error_reason(exc)
            print(f"[capture] 原位浮窗无法打开: {msg}", flush=True)
            with self._lock:
                if self._generation == gen:
                    self._overlay_opening = False
            if self._is_current(gen):
                self.status.error(ErrorCode.dependency_failed, f"原位浮窗无法打开：{msg}")
            return

        overlay_box["handle"] = overlay
        stale = False
        with self._lock:
            if self._generation != gen:
                stale = True
            else:
                self._overlay_opening = False
                self._overlay = overlay
        if stale:
            _close_handle(overlay)
            return

        self._spawn(lambda: self._run_inplace(gen, mode, overlay, cropped_png))

    def _run_inplace(self, gen: int, mode: CaptureMode, overlay: OverlayHandle, cropped_png: bytes) -> None:
        """后台：识别（翻译模式再翻译）后把结果或失败原因交给浮窗；浮窗已关或已被新一次截屏取代则丢弃。"""
        try:
            target_lang = self._target_lang() if mode == "translate" else "raw"
            resp = self.api.ocr_inplace(cropped_png, target_lang=target_lang)
            blocks = _parse_blocks(resp["blocks"])
            full_text, full_ocr = str(resp["text"]), str(resp["ocr_text"])
        except Exception as exc:
            msg = api_error_reason(exc)
            default = ErrorCode.translate_failed if mode == "translate" else ErrorCode.ocr_failed
            kind = "翻译" if mode == "translate" else "识别"
            print(f"[capture] 原位{kind}失败 ({api_error_code(exc, default).value}): {msg}", flush=True)
            self._apply_to_overlay(gen, overlay, lambda: overlay.set_error(msg))
            return
        self._apply_to_overlay(gen, overlay, lambda: overlay.set_result(blocks, full_text, full_ocr))

    def _target_lang(self) -> str:
        cfg = self.api.get_config() or {}
        return str(cfg.get("translate_target_lang") or "zh").strip() or "zh"

    def _apply_to_overlay(self, gen: int, overlay: OverlayHandle, update: Callable[[], None]) -> None:
        if not self._is_current(gen):
            return

        def _apply() -> None:
            with self._lock:
                active = self._generation == gen and self._overlay is overlay
            if active:
                update()

        self.invoke_ui(_apply)

    def _handle_copy_text(self, text: str) -> bool:
        """浮窗菜单复制：返回是否写入成功（浮窗显示「✓ 已复制」或「复制失败」），不发 status 事件。"""
        try:
            return bool(self.copy_clipboard(text))
        except Exception as exc:
            print(f"[capture] 剪贴板写入异常: {api_error_reason(exc)}", flush=True)
            return False
