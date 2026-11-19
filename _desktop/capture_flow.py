"""截屏原位翻译（Ctrl+Alt+X）与纯 OCR 自动复制（Ctrl+Alt+O）统一业务编排。

两大入口 100% 复用同一套「直接透明全屏红十字框选 → 框选后仅截取选中区域」链路：
- 按下快捷键：立即弹出透明全屏 `_ui_desktop.RedCrosshairSelector`（不预先全屏截图、零跳动、零闪屏；
  若正在框选或抓屏中则直接忽略重复触发，防长按连发风暴）；
- `trigger_translate()`：松手关闭框选器 → 短暂等待合成器卸载遮罩 → 仅截取选区 PNG →
  打开 `InplaceTranslateOverlay(loading=True)` → 后台 `POST /api/v1/ocr-inplace` 渲染原位背景遮挡译文；
- `trigger_ocr()`：松手关闭框选器 → 仅截取选区 PNG → 后台 `POST /api/v1/ocr-inplace(target_lang="raw")` →
  自动写入剪贴板并在 UI 线程展示成功字数 Toast（失败可见报错并落日志）。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from _contracts import ErrorCode, OcrTextBlock
from _desktop.api import ApiError, api_error_code, api_error_reason
from _desktop.status import PlatformStatus

RectTuple = tuple[int, int, int, int]
DEFAULT_CAPTURE_DELAY_S = 0.08


class SelectorHandle(Protocol):
    def close(self) -> Any: ...


class OverlayHandle(Protocol):
    def set_loading(self) -> None: ...

    def set_result(
        self,
        blocks: list[OcrTextBlock],
        full_translated_text: str = "",
        full_ocr_text: str = "",
    ) -> None: ...

    def set_error(self, message: str) -> None: ...

    def close(self) -> Any: ...


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
        loading: bool,
        on_copy_text: Callable[[str], None],
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
    """截屏原位翻译与纯 OCR 的共享业务编排器（先透明框选，再仅截取选区画面）。"""

    TRANSLATE_MODE_LABEL = "截屏原位翻译"
    OCR_MODE_LABEL = "纯 OCR 识别 · 自动复制"

    def __init__(
        self,
        *,
        capture_region: Callable[[RectTuple, RectTuple | None], bytes],
        open_selector: OpenSelector,
        open_inplace_overlay: OpenInplaceOverlay,
        copy_clipboard: Callable[[str], bool],
        notify_toast: Callable[[str, str], None],
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
        self.notify_toast = notify_toast
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
        """Ctrl+Alt+X：复用红十字框选器，松手后仅截选区并原位渲染背景遮挡译文浮窗。"""
        self._start_capture("translate", self.TRANSLATE_MODE_LABEL)

    def trigger_ocr(self) -> None:
        """Ctrl+Alt+O：复用红十字框选器，松手后仅截选区做纯 OCR 并自动写入剪贴板。"""
        self._start_capture("ocr", self.OCR_MODE_LABEL)

    def overlay_active(self) -> bool:
        with self._lock:
            return self._selector is not None or self._overlay is not None or self._selector_opening

    def close_overlay(self, *, reset_status: bool = True) -> bool:
        """Esc / 任意键 / 新触发：关闭活跃选区或原位浮窗并失效在途后台请求。"""
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
        if had_active and reset_status:
            self.status.idle()
        return False

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _start_capture(self, mode: Literal["translate", "ocr"], mode_label: str) -> None:
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
                self.status.idle()

            def _on_selected(rect: RectTuple) -> None:
                with self._lock:
                    if self._generation != gen:
                        return
                    self._selector = None
                    self._selector_opening = False
                    self._capturing = True
                self.status.busy()
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
        mode: Literal["translate", "ocr"],
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

        if mode == "translate":
            with self._lock:
                if self._generation != gen:
                    return
                self._overlay_opening = True
            self.invoke_ui(lambda: self._handle_translate_selected(gen, png, rect))
        else:
            if not self._is_current(gen):
                return
            self._run_ocr_worker(gen, png)

    def _handle_translate_selected(self, gen: int, cropped_png: bytes, rect: RectTuple) -> None:
        if not self._is_current(gen):
            return
        overlay_box: dict[str, OverlayHandle | None] = {"handle": None}

        def _on_closed() -> None:
            should_idle = False
            with self._lock:
                if self._generation == gen and self._overlay is overlay_box["handle"]:
                    self._overlay = None
                    self._generation += 1
                    should_idle = True
            if should_idle:
                self.status.idle()

        def _on_retry() -> None:
            handle = overlay_box["handle"]
            if handle is None or not self._is_current(gen):
                return
            handle.set_loading()
            self.status.busy()
            self._spawn(lambda: self._run_inplace_translate(gen, handle, cropped_png))

        try:
            overlay = self.open_inplace_overlay(
                cropped_png,
                rect,
                loading=True,
                on_copy_text=self._handle_copy_text,
                on_retry=_on_retry,
                on_closed=_on_closed,
            )
        except Exception as exc:
            msg = api_error_reason(exc)
            print(f"[capture] 原位译文浮窗无法打开: {msg}", flush=True)
            with self._lock:
                if self._generation == gen:
                    self._overlay_opening = False
            if self._is_current(gen):
                self.status.error(ErrorCode.dependency_failed, f"原位译文浮窗无法打开：{msg}")
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

        self._spawn(lambda: self._run_inplace_translate(gen, overlay, cropped_png))

    def _run_inplace_translate(self, gen: int, overlay: OverlayHandle, cropped_png: bytes) -> None:
        try:
            cfg = self.api.get_config() or {}
            target_lang = str(cfg.get("translate_target_lang") or "zh").strip() or "zh"
            resp = self.api.ocr_inplace(cropped_png, target_lang=target_lang)
            blocks = _parse_blocks(resp.get("blocks"))
            full_text = str(resp.get("text") or "")
            full_ocr = str(resp.get("ocr_text") or "")
        except Exception as exc:
            msg = api_error_reason(exc)
            code = api_error_code(exc, ErrorCode.translate_failed)
            print(f"[capture] 截屏原位翻译失败 ({code.value}): {msg}", flush=True)
            if self._is_current(gen):
                def _apply_err() -> None:
                    with self._lock:
                        active = self._generation == gen and self._overlay is overlay
                    if active:
                        overlay.set_error(msg)

                self.invoke_ui(_apply_err)
                self.status.error(code, f"截屏原位翻译失败：{msg}")
            return

        if not self._is_current(gen):
            return

        def _apply_ok() -> None:
            with self._lock:
                active = self._generation == gen and self._overlay is overlay
            if active:
                overlay.set_result(blocks, full_text, full_ocr)

        self.invoke_ui(_apply_ok)
        self.status.idle()

    def _run_ocr_worker(self, gen: int, cropped_png: bytes) -> None:
        try:
            resp = self.api.ocr_inplace(cropped_png, target_lang="raw")
            ocr_text = str(resp.get("ocr_text") or resp.get("text") or "").strip()
            if not ocr_text:
                raise ApiError(503, ErrorCode.ocr_failed.value, "未识别到文字")
        except Exception as exc:
            msg = api_error_reason(exc)
            code = api_error_code(exc, ErrorCode.ocr_failed)
            print(f"[capture] OCR 识别失败 ({code.value}): {msg}", flush=True)
            if self._is_current(gen):
                self.status.error(code, f"OCR 识别失败：{msg}")
            return

        if not self._is_current(gen):
            return

        copied = False
        try:
            copied = bool(self.copy_clipboard(ocr_text))
        except Exception as exc:
            msg = api_error_reason(exc)
            print(f"[capture] OCR 剪贴板写入异常: {msg}", flush=True)
            self.status.error(ErrorCode.dependency_failed, f"OCR 文本写入剪贴板失败：{msg}")
            return

        if not copied:
            print("[capture] OCR 剪贴板写入返回失败", flush=True)
            self.status.error(ErrorCode.dependency_failed, "OCR 文本写入剪贴板失败")
            return

        self.status.idle()
        toast_msg = f"已复制 OCR 文本（{len(ocr_text)} 字）"
        self.invoke_ui(lambda: self.notify_toast(toast_msg, "ok"))

    def _handle_copy_text(self, text: str) -> None:
        try:
            ok = bool(self.copy_clipboard(text))
        except Exception as exc:
            self.status.error(ErrorCode.dependency_failed, f"剪贴板写入失败：{api_error_reason(exc)}")
            return
        if not ok:
            self.status.error(ErrorCode.dependency_failed, "剪贴板写入失败")
