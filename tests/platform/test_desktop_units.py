"""桌面业务编排与平台辅助单元：CaptureFlow（红十字框选复用原位翻译 + 纯 OCR）、VoiceFlow（热词 + 防409排队）、
DesktopActions 四键、ProductEventLoop 断线退避与 HUD 映射、跨平台单例锁、冲突扫描、启动检查。

全部假对象：不开图形窗口、不截屏、不开麦、不读真实 gsettings、不连 systemd。
"""
from __future__ import annotations

import io
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from _capture import FakeAudioSource
from _contracts import ComponentStatus, ErrorCode, OcrTextBlock
from _desktop import capture_flow as capture_flow_mod
from _desktop.actions import DesktopActions
from _desktop.api import ApiError
from _desktop.capture_flow import OLD_OVERLAY_DISMISS_DELAY_S, CaptureFlow, SaveResult
from _desktop.delivery import LastDelivered, RepeatAction
from _desktop.event_stream import ProductEventLoop, dispatch_product_event
from _desktop.singleton import try_acquire_singleton_lock
from _desktop.status import PlatformStatus
from _desktop.voice import VoiceFlow, build_keywords_context_provider
from _focus import StaticFocusProbe
from _hotkey import HotkeyBindings
from _linux.conflicts import ConflictScan, scan_hotkey_conflicts
from _linux.startup_check import startup_issues
from _platform.protocol import ScreenCapture

DEFAULTS = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r")


def _sample_screen_png(
    width: int = 384,
    height: int = 216,
    color: tuple[int, int, int] = (32, 64, 96),
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


_DEFAULT_SCREEN_PNG = _sample_screen_png()


def _status():
    posted: list[dict] = []
    return PlatformStatus(posted.append, post_components=posted.append, start_thread=False), posted


class FakeSelector:
    def __init__(self, frozen_png, rect, mode_label, on_selected, on_cancelled, *, trace=None) -> None:
        self.frozen_png = frozen_png
        self.rect = rect
        self.mode_label = mode_label
        self.on_selected = on_selected
        self.on_cancelled = on_cancelled
        self.closed = False
        self.closed_silent = False
        self._trace = trace

    def close_silent(self) -> None:
        self.closed = True
        self.closed_silent = True
        if self._trace is not None:
            self._trace.append("selector.close_silent")

    def close(self) -> None:
        self.closed = True
        if self._trace is not None:
            self._trace.append("selector.close")


class FakeInplaceOverlay:
    def __init__(self, png, rect, *, mode, virtual_rect, on_copy_text, on_retry, on_closed, trace=None) -> None:
        self.png = png
        self.rect = rect
        self.mode = mode
        self.virtual_rect = virtual_rect
        self.loading = True  # 创建即加载态
        self.on_copy_text = on_copy_text
        self.on_retry = on_retry
        self.on_closed = on_closed
        self.blocks: list[OcrTextBlock] = []
        self.full_text = ""
        self.full_ocr = ""
        self.error: str | None = None
        self.closed = False
        self.closed_silent = False
        self.escape_calls = 0
        self.raised = 0
        self.activated = 0
        self._trace = trace

    def set_loading(self) -> None:
        self.loading = True
        self.error = None

    def set_result(self, blocks: list[OcrTextBlock], full_translated_text: str, full_ocr_text: str) -> None:
        self.loading = False
        self.error = None
        self.blocks = list(blocks)
        self.full_text = full_translated_text
        self.full_ocr = full_ocr_text

    def set_error(self, message: str) -> None:
        self.loading = False
        self.error = message

    def handle_escape(self) -> None:
        self.escape_calls += 1
        self.close()

    def raise_(self) -> None:
        self.raised += 1
        if self._trace is not None:
            self._trace.append("overlay.raise_")

    def activateWindow(self) -> None:
        self.activated += 1
        if self._trace is not None:
            self._trace.append("overlay.activateWindow")

    def close_silent(self) -> None:
        self.closed = True
        self.closed_silent = True
        if self._trace is not None:
            self._trace.append("overlay.close_silent")

    def close(self) -> None:
        self.closed = True
        if self.on_closed:
            self.on_closed()


class FakeAnnotator:
    def __init__(
        self,
        png,
        rect,
        *,
        virtual_rect,
        initial_style,
        on_style_changed,
        on_copy_image,
        on_save_image,
        on_closed,
        two_step_escape: bool = False,
        trace=None,
    ) -> None:
        self.png = png
        self.rect = rect
        self.virtual_rect = virtual_rect
        self.initial_style = initial_style
        self.on_style_changed = on_style_changed
        self.on_copy_image = on_copy_image
        self.on_save_image = on_save_image
        self.on_closed = on_closed
        self.text_editing = two_step_escape
        self.escape_calls = 0
        self.closed = False
        self.closed_silent = False
        self.raised = 0
        self.activated = 0
        self._trace = trace

    def handle_escape(self) -> None:
        self.escape_calls += 1
        if self.text_editing:
            self.text_editing = False
            return
        self.close()

    def raise_(self) -> None:
        self.raised += 1
        if self._trace is not None:
            self._trace.append("annotator.raise_")

    def activateWindow(self) -> None:
        self.activated += 1
        if self._trace is not None:
            self._trace.append("annotator.activateWindow")

    def close_silent(self) -> None:
        self.closed = True
        self.closed_silent = True
        if self._trace is not None:
            self._trace.append("annotator.close_silent")

    def close(self) -> None:
        self.closed = True
        if self.on_closed:
            self.on_closed()


def _make_capture_harness(
    api,
    *,
    capture_fn=None,
    copy_fn=None,
    copy_image_fn=None,
    save_dir_fn=None,
    now_fn=None,
    two_step_escape: bool = False,
    get_virtual_rect_fn=None,
    old_overlay_dismiss_delay_s: float = OLD_OVERLAY_DISMISS_DELAY_S,
    sleep_fn=None,
):
    status, posted = _status()
    selectors: list[FakeSelector] = []
    overlays: list[FakeInplaceOverlay] = []
    annotators: list[FakeAnnotator] = []
    copied: list[str] = []
    screen_calls: list[int] = []
    sleep_calls: list[float] = []
    trace: list[str] = []

    def open_selector(frozen_png, rect, mode_label, on_selected, on_cancelled):
        trace.append("open_selector")
        sel = FakeSelector(frozen_png, rect, mode_label, on_selected, on_cancelled, trace=trace)
        selectors.append(sel)
        return sel

    def open_overlay(png, rect, **kwargs):
        trace.append("open_overlay")
        ov = FakeInplaceOverlay(png, rect, trace=trace, **kwargs)
        overlays.append(ov)
        return ov

    def open_annotator(png, rect, **kwargs):
        trace.append("open_annotator")
        ann = FakeAnnotator(png, rect, two_step_escape=two_step_escape, trace=trace, **kwargs)
        annotators.append(ann)
        return ann

    def default_copy(text: str) -> bool:
        copied.append(text)
        return True

    def default_capture_screen() -> ScreenCapture:
        trace.append("capture_screen")
        screen_calls.append(len(screen_calls) + 1)
        return ScreenCapture(png_bytes=_DEFAULT_SCREEN_PNG, virtual_rect=(0, 0, 1920, 1080))

    def record_sleep(delay: float) -> None:
        trace.append("sleep")
        sleep_calls.append(delay)
        if sleep_fn is not None:
            sleep_fn(delay)

    flow = CaptureFlow(
        capture_screen=capture_fn or default_capture_screen,
        open_selector=open_selector,
        open_inplace_overlay=open_overlay,
        open_annotator=open_annotator,
        copy_clipboard=copy_fn or default_copy,
        copy_image=copy_image_fn,
        save_dir=save_dir_fn,
        now=now_fn or datetime.now,
        api=api,
        status=status,
        invoke_ui=lambda fn: fn(),
        get_virtual_rect=get_virtual_rect_fn or (lambda: (0, 0, 1920, 1080)),
        old_overlay_dismiss_delay_s=old_overlay_dismiss_delay_s,
        sleep=record_sleep,
        spawn=lambda fn: fn(),
    )
    flow._test_screen_calls = screen_calls  # type: ignore[attr-defined]
    flow._test_sleep_calls = sleep_calls  # type: ignore[attr-defined]
    flow._test_trace = trace  # type: ignore[attr-defined]
    flow._test_annotators = annotators  # type: ignore[attr-defined]
    return flow, status, posted, selectors, overlays, copied


def _ocr_payload(text: str, mode: str = "raw", lang_pair: str | None = None) -> dict:
    """/api/v1/ocr-inplace 的单块响应（契约 OcrInplaceResponse；raw 时译文 = 原文）。"""
    return {
        "ok": True,
        "text": text,
        "ocr_text": text,
        "mode": mode,
        "lang_pair": lang_pair,
        "source_lang": "en" if mode == "translate" else None,
        "target_lang": "zh" if mode == "translate" else None,
        "direction_mode": "auto" if mode == "translate" else None,
        "image_width": 200,
        "image_height": 80,
        "blocks": [{
            "bbox": [4, 6, 100, 24],
            "lines": [{"bbox": [4, 6, 100, 24], "text": text, "soft_wrap": False}],
            "ocr_text": text,
            "translated_text": text,
        }],
    }


# ------------------------------------------------------------------ CaptureFlow


def test_capture_flow_translate_reuses_selector_and_renders_inplace_blocks():
    assert not hasattr(capture_flow_mod, "DEFAULT_CAPTURE_DELAY_S")

    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None, bytes]] = []

        def get_config(self) -> dict:
            # 用非默认语言对验证 CaptureFlow 真实透传配置中的 translate_lang_pair 而非落回默认
            return {"translate_lang_pair": "zh-ja"}

        def ocr_inplace(
            self,
            png: bytes,
            *,
            mode: str = "translate",
            lang_pair: str | None = None,
            source_lang: str | None = None,
            target_lang: str | None = None,
            lang=None,
        ) -> dict:
            del source_lang, target_lang, lang
            self.calls.append((mode, lang_pair, png))
            return {
                "ok": True,
                "text": "Hello World",
                "ocr_text": "你好世界",
                "mode": mode,
                "lang_pair": lang_pair,
                "source_lang": "zh",
                "target_lang": "en",
                "direction_mode": "auto",
                "image_width": 200,
                "image_height": 80,
                "blocks": [
                    {
                        "bbox": [4, 6, 100, 24],
                        "lines": [{"bbox": [4, 6, 100, 24], "text": "你好世界", "soft_wrap": False}],
                        "ocr_text": "你好世界",
                        "translated_text": "Hello World",
                        "bg_color": "#112233",
                        "fg_color": "#ffffff",
                    }
                ],
            }

    api = Api()
    flow, status, posted, selectors, overlays, copied = _make_capture_harness(api)

    flow.trigger_translate()
    assert len(selectors) == 1
    assert selectors[0].frozen_png == _DEFAULT_SCREEN_PNG
    assert selectors[0].mode_label == "截屏原位翻译"
    assert selectors[0].rect == (0, 0, 1920, 1080)
    assert flow._test_screen_calls == [1]  # type: ignore[attr-defined]
    assert flow._test_sleep_calls == []  # type: ignore[attr-defined]  # 常态无旧浮窗 0 等待
    assert flow.overlay_active() is True

    selectors[0].on_selected((100, 200, 300, 120))
    status.drain()
    assert len(overlays) == 1
    ov = overlays[0]
    assert ov.loading is False and ov.error is None
    assert ov.mode == "translate" and ov.virtual_rect == (0, 0, 1920, 1080)
    assert ov.rect == (100, 200, 300, 120)
    assert len(ov.blocks) == 1 and isinstance(ov.blocks[0], OcrTextBlock)
    assert ov.blocks[0].bg_color == "#112233" and ov.full_text == "Hello World" and ov.full_ocr == "你好世界"
    # 松手后不再二次抓屏，直接从内存冻结图裁出有效选区 PNG（300/1920*384=60, 120/1080*216=24）
    assert flow._test_screen_calls == [1]  # type: ignore[attr-defined]
    assert flow._test_sleep_calls == []  # type: ignore[attr-defined]
    assert len(api.calls) == 1 and api.calls[0][:2] == ("translate", "zh-ja")
    assert api.calls[0][2].startswith(b"\x89PNG")
    with Image.open(io.BytesIO(api.calls[0][2])) as cropped:
        assert cropped.size == (60, 24)
    assert selectors[0].closed_silent is True
    assert ov.raised == 1 and ov.activated == 1

    # 菜单「复制译文」走平台剪贴板，并把写入结果交回浮窗
    assert ov.on_copy_text(ov.full_text) is True
    assert copied == ["Hello World"]

    # 重试按钮重新触发 ocr_inplace
    ov.on_retry()
    assert len(api.calls) == 2

    # 关闭旧浮窗
    flow.close_overlay()
    assert ov.closed is True and flow.overlay_active() is False


def test_capture_flow_translate_error_shows_only_the_reason_on_the_overlay():
    class FailingApi:
        def get_config(self) -> dict:
            return {"translate_lang_pair": "zh-en"}

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            raise ApiError(503, ErrorCode.translate_failed.value, "Ollama 17947 未启动")

    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(FailingApi())
    flow.trigger_translate()
    selectors[0].on_selected((10, 20, 200, 90))
    status.drain()

    assert overlays[0].loading is False
    assert overlays[0].error == "Ollama 17947 未启动"  # 只传原因，「翻译失败：」前缀由浮窗按模式加


def test_capture_flow_ocr_opens_the_same_overlay_and_copies_only_on_request():
    """T16：纯 OCR 打开同一个浮窗（mode=ocr、请求 mode=raw），不自动写剪贴板；菜单复制写原文全文并返回成败。"""

    class OcrApi:
        def __init__(self) -> None:
            self.modes: list[tuple[str, str | None]] = []

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            self.modes.append((mode, lang_pair))
            return _ocr_payload("跨平台纯OCR", mode="raw")

    results = iter([True, False])

    def copy(text: str) -> bool:
        copies.append(text)
        outcome = next(results, None)
        if outcome is None:
            raise RuntimeError("xclip 不可用")
        return outcome

    copies: list[str] = []
    api = OcrApi()
    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(api, copy_fn=copy)
    flow.trigger_ocr()
    assert selectors[0].mode_label == "纯 OCR 识别"
    selectors[0].on_selected((10, 10, 100, 40))
    ov = overlays[0]
    assert ov.mode == "ocr" and api.modes == [("raw", None)]
    assert ov.full_ocr == "跨平台纯OCR" and ov.error is None
    assert copies == []  # 不自动复制
    assert [ov.on_copy_text(ov.full_ocr) for _ in range(3)] == [True, False, False]  # 成功 / 返回失败 / 抛异常
    assert copies == ["跨平台纯OCR"] * 3
    status.drain()
    assert posted == []  # 复制失败只在浮窗显示，不发 status 事件


def test_capture_flow_ocr_failure_shows_in_the_overlay_and_cancel_closes_everything():
    class EmptyOcrApi:
        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            raise ApiError(503, ErrorCode.ocr_failed.value, "没有识别到文字")

    flow, status, posted, selectors, overlays, copied = _make_capture_harness(EmptyOcrApi())
    flow.trigger_ocr()
    selectors[0].on_cancelled()
    assert flow.overlay_active() is False and overlays == []

    flow.trigger_ocr()
    selectors[-1].on_selected((0, 0, 50, 50))
    assert overlays[0].mode == "ocr" and overlays[0].error == "没有识别到文字"
    assert copied == []


def test_capture_flows_send_no_busy_or_idle_and_only_overlay_less_failures_send_error():
    """T17：翻译与 OCR 全流程（框选、取消、截屏、加载、成功、重试失败、关闭）不发 busy/idle；
    只有没有浮窗可显示的失败（框选器打不开、浮窗打不开；截屏失败见下一条用例）才发 error。"""
    replies = iter(["ok", "fail"] * 2)

    class Api:
        def get_config(self) -> dict:
            return {"translate_lang_pair": "zh-en"}

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            if next(replies) == "fail":
                raise ApiError(503, ErrorCode.translate_failed.value, "Ollama 未响应")
            return _ocr_payload("Hello", mode=mode, lang_pair=lang_pair)

    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(Api())
    for trigger in (flow.trigger_translate, flow.trigger_ocr):
        trigger()
        selectors[-1].on_cancelled()
        trigger()
        selectors[-1].on_selected((0, 0, 100, 40))  # 截屏 → 加载 → 成功
        overlays[-1].on_retry()  # 重试 → 失败（只在浮窗显示）
        assert overlays[-1].error == "Ollama 未响应"
        overlays[-1].close()
    status.drain()
    assert posted == []

    def broken(*_args, **_kwargs):
        raise RuntimeError("no display")

    working_selector = flow.open_selector
    flow.open_selector = broken
    flow.trigger_translate()
    flow.open_selector = working_selector
    flow.open_inplace_overlay = broken
    flow.trigger_ocr()
    selectors[-1].on_selected((0, 0, 100, 40))
    status.drain()
    assert [(p["event"], p["message"]) for p in posted] == [
        ("error", "框选器无法打开：no display"),
        ("error", "原位浮窗无法打开：no display"),
    ]


def test_capture_flow_frozen_capture_storm_guard_and_seamless_handoff_order():
    """根治回归：按键先冻结全屏（常态 0 等待、连按 20 次仅抓 1 次开 1 窗）、松手 0 二次抓屏且严格按
    crop -> open_overlay -> selector.close_silent -> overlay.raise_ -> overlay.activateWindow 交接。"""
    class Api:
        def __init__(self) -> None:
            self.seen_pngs: list[bytes] = []

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            self.seen_pngs.append(png)
            return _ocr_payload("冻结裁切OCR", mode=mode, lang_pair=lang_pair)

    api = Api()
    flow, status, posted, selectors, overlays, copied = _make_capture_harness(api)

    # 连续触发 20 次（模拟长按快捷键连发）→ 仅抓 1 次全屏、开 1 个冻结框选器、0 次 sleep
    for _ in range(20):
        flow.trigger_ocr()
    assert len(selectors) == 1
    assert flow._test_screen_calls == [1]  # type: ignore[attr-defined]
    assert flow._test_sleep_calls == []  # type: ignore[attr-defined]

    # 松手零延迟从内存冻结图裁切 → 先开浮窗 → finally 静默关旧 selector → 重新 raise_/activateWindow
    selectors[0].on_selected((120, 240, 360, 90))
    status.drain()
    assert flow._test_screen_calls == [1]  # type: ignore[attr-defined]
    assert flow._test_sleep_calls == []  # type: ignore[attr-defined]
    assert flow._test_trace == [  # type: ignore[attr-defined]
        "capture_screen",
        "open_selector",
        "open_overlay",
        "selector.close_silent",
        "overlay.raise_",
        "overlay.activateWindow",
    ]
    assert len(api.seen_pngs) == 1 and api.seen_pngs[0].startswith(b"\x89PNG")
    assert len(overlays) == 1 and overlays[0].full_ocr == "冻结裁切OCR"


def test_capture_screen_failure_is_visible():
    def boom():
        raise RuntimeError("Portal 拒绝授权")

    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(api=object(), capture_fn=boom)
    flow.trigger_translate()
    status.drain()
    assert selectors == []
    assert overlays == []
    assert flow.overlay_active() is False
    assert posted[-1]["event"] == "error" and "Portal 拒绝授权" in posted[-1]["message"]

    # capture_screen 返回空 PNG 同样可见报错并复位 _capturing
    flow_empty, status2, posted2, selectors2, overlays2, _c2 = _make_capture_harness(
        api=object(),
        capture_fn=lambda: ScreenCapture(png_bytes=b"", virtual_rect=(0, 0, 1920, 1080)),
    )
    flow_empty.trigger_ocr()
    status2.drain()
    assert selectors2 == [] and overlays2 == [] and flow_empty.overlay_active() is False
    assert posted2[-1]["event"] == "error" and "空" in posted2[-1]["message"]


def test_capture_flow_reentrant_close_no_deadlock_and_generation_isolation():
    """P1-4 回归：过期 selector/overlay 的 close() 重入回调不死锁，且 ReentrantSelector 在交接 finally 关闭时不误清目标浮窗。"""
    status, posted = _status()
    ui_queue: list = []
    selector_handles: list = []
    overlay_handles: list = []

    class ReentrantSelector:
        """故意只提供 close()（无 close_silent），在 close() 内同步回调 on_cancelled 重入 CaptureFlow._lock。"""

        def __init__(self, frozen_png, on_selected, on_cancelled) -> None:
            self.frozen_png = frozen_png
            self.on_selected = on_selected
            self.on_cancelled = on_cancelled
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self.on_cancelled()

    class ReentrantOverlay:
        """故意只提供 close()（无 close_silent），在 close() 内同步回调 on_closed 重入 CaptureFlow._lock。"""

        def __init__(self, on_closed) -> None:
            self.on_closed = on_closed
            self.closed = False
            self.raised = 0
            self.activated = 0

        def set_loading(self) -> None:
            pass

        def set_result(self, blocks, full_translated_text, full_ocr_text) -> None:
            pass

        def set_error(self, message: str) -> None:
            pass

        def raise_(self) -> None:
            self.raised += 1

        def activateWindow(self) -> None:
            self.activated += 1

        def close(self) -> None:
            self.closed = True
            self.on_closed()

    selector_stage = {"n": 0}
    cancel_in_open_overlay = {"enabled": True}

    def open_selector(frozen_png, _rect, _label, on_selected, on_cancelled):
        selector_stage["n"] += 1
        sel = ReentrantSelector(frozen_png, on_selected, on_cancelled)
        selector_handles.append(sel)
        if selector_stage["n"] == 1:
            # 模拟代次 1 打开框选器途中被取消并立刻开启代次 2（代次 2 仍在 ui_queue 中待准备/打开）
            flow.close_overlay()
            flow.trigger_translate()
        return sel

    def open_overlay(_png, _rect, *, mode, virtual_rect, on_copy_text, on_retry, on_closed):
        del mode, virtual_rect, on_copy_text, on_retry
        # 交接开窗期间 _overlay_opening=True，重入触发必须被拦截
        before_q = len(ui_queue)
        flow.trigger_ocr()
        assert len(ui_queue) == before_q
        ov = ReentrantOverlay(on_closed)
        overlay_handles.append(ov)
        if cancel_in_open_overlay["enabled"]:
            # 模拟浮窗打开途中被 Esc / close_overlay 取消，令当前代次过期
            flow.close_overlay()
        return ov

    class DummyApi:
        def get_config(self) -> dict:
            return {"translate_lang_pair": "zh-en"}

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            del png, lang
            return _ocr_payload("OK", mode=mode, lang_pair=lang_pair)

    flow = CaptureFlow(
        capture_screen=lambda: ScreenCapture(png_bytes=_DEFAULT_SCREEN_PNG, virtual_rect=(0, 0, 1920, 1080)),
        open_selector=open_selector,
        open_inplace_overlay=open_overlay,
        copy_clipboard=lambda _t: True,
        api=DummyApi(),
        status=status,
        invoke_ui=ui_queue.append,
        get_virtual_rect=lambda: (0, 0, 1920, 1080),
        old_overlay_dismiss_delay_s=0.0,
        spawn=lambda fn: fn(),
    )

    # 1. 触发代次 1：尚未落到 UI 线程时 overlay_active() 就应为 True（含 _capturing）
    flow.trigger_translate()
    assert flow.overlay_active() is True
    assert len(ui_queue) == 1
    # 执行 _prepare_ui（抓全屏后排队 _open_selector_ui）
    ui_queue.pop(0)()
    assert flow._selector_opening is True
    assert len(ui_queue) == 1

    # 执行代次 1 的 _open_selector_ui：内部取消代次 1 并启动代次 2 → 代次 1 过期关闭不死锁，且不误清代次 2 的 _capturing
    ui_queue.pop(0)()
    assert selector_handles[0].closed is True
    assert flow._capturing is True
    assert flow.overlay_active() is True
    assert len(ui_queue) == 1  # 代次 2 的 _prepare_ui
    flow.trigger_ocr()
    assert len(ui_queue) == 1

    # 2. 执行代次 2 的 _prepare_ui + _open_selector_ui 并完成框选 → open_overlay 内取消 → 过期 overlay 与 selector 均关闭无死锁
    ui_queue.pop(0)()
    ui_queue.pop(0)()
    assert flow._selector_opening is False
    assert len(selector_handles) == 2 and selector_handles[1].closed is False

    selector_handles[1].on_selected((10, 20, 100, 80))
    assert len(overlay_handles) == 1 and overlay_handles[0].closed is True
    assert overlay_handles[0].raised == 0 and overlay_handles[0].activated == 0
    assert selector_handles[1].closed is True
    assert flow.overlay_active() is False

    # 3. 正常松手交接下，ReentrantSelector.close() 同步触发 _on_cancelled 不得清除目标浮窗或代次，且目标浮窗成功复焦
    cancel_in_open_overlay["enabled"] = False
    flow.trigger_translate()
    ui_queue.pop(0)()  # _prepare_ui
    ui_queue.pop(0)()  # _open_selector_ui
    selector_handles[-1].on_selected((10, 20, 100, 80))
    assert selector_handles[-1].closed is True
    assert overlay_handles[-1].closed is False
    assert overlay_handles[-1].raised == 1 and overlay_handles[-1].activated == 1
    assert flow.overlay_active() is True
    assert flow._overlay is overlay_handles[-1]


def test_capture_flow_old_overlay_dismiss_cancellation_and_exception_cleanup():
    """回归：old_overlay 退场有序等待与取消、抓屏中取消、以及裁切/开窗/准备/派发异常下 selector 必关闭且状态复位。"""
    # 1. 替换旧浮窗：严格按 old_overlay.close_silent -> sleep(0.06) -> capture_screen 顺序；常态无旧窗 0 sleep
    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(
        api=SimpleNamespace(
            get_config=lambda: {"translate_lang_pair": "zh-en"},
            ocr_inplace=lambda *a, **kw: _ocr_payload("OK", mode="raw"),
        ),
        old_overlay_dismiss_delay_s=OLD_OVERLAY_DISMISS_DELAY_S,
    )
    flow.trigger_ocr()
    assert flow._test_sleep_calls == []  # type: ignore[attr-defined]
    selectors[-1].on_selected((10, 20, 120, 80))
    first_ov = overlays[-1]
    assert first_ov.closed is False

    flow._test_trace.clear()  # type: ignore[attr-defined]
    flow.trigger_ocr()
    assert first_ov.closed_silent is True
    assert flow._test_sleep_calls == [OLD_OVERLAY_DISMISS_DELAY_S]  # type: ignore[attr-defined]
    assert flow._test_trace == ["overlay.close_silent", "sleep", "capture_screen", "open_selector"]  # type: ignore[attr-defined]

    # 2. 在 old_overlay 退场 sleep 中取消（handle_escape / close_overlay）→ 不调 capture_screen、不开框选器
    selectors[-1].on_selected((10, 20, 120, 80))
    cancel_calls: list[int] = []

    def cancel_in_sleep(_delay: float) -> None:
        assert flow._capturing is True
        flow.handle_escape()

    flow._sleep = cancel_in_sleep
    orig_capture = flow.capture_screen
    flow.capture_screen = lambda: (cancel_calls.append(1) or orig_capture())
    before_sel_count = len(selectors)
    flow.trigger_ocr()
    assert cancel_calls == []
    assert len(selectors) == before_sel_count
    assert flow._capturing is False and flow.overlay_active() is False

    # 3. 在 capture_screen 执行中取消 → 丢弃迟到全屏图，不开框选器
    flow._sleep = lambda _d: None
    flow.capture_screen = lambda: (flow.close_overlay(), ScreenCapture(_DEFAULT_SCREEN_PNG, (0, 0, 1920, 1080)))[1]
    flow.trigger_ocr()
    assert len(selectors) == before_sel_count
    assert flow.overlay_active() is False

    # 4. 松手交接各异常分支（裁切失败 / open_inplace_overlay 抛错 / open_annotator 抛错或为 None）均在 finally 必关 selector 且报错可见
    flow.capture_screen = orig_capture
    # 4a. 裁切失败（非法宽 0 选区）
    flow.trigger_ocr()
    bad_sel_crop = selectors[-1]
    bad_sel_crop.on_selected((10, 20, 0, 80))
    status.drain()
    assert bad_sel_crop.closed_silent is True
    assert flow._overlay_opening is False and flow.overlay_active() is False
    assert posted[-1]["event"] == "error" and "截屏失败" in posted[-1]["message"]

    # 4b. open_inplace_overlay 抛错
    orig_open_ov = flow.open_inplace_overlay
    flow.open_inplace_overlay = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("overlay boom"))
    flow.trigger_ocr()
    bad_sel_ov = selectors[-1]
    bad_sel_ov.on_selected((10, 20, 120, 80))
    status.drain()
    assert bad_sel_ov.closed_silent is True
    assert flow._overlay_opening is False and flow.overlay_active() is False
    assert posted[-1]["event"] == "error" and "原位浮窗无法打开：overlay boom" == posted[-1]["message"]
    flow.open_inplace_overlay = orig_open_ov

    # 4c. open_annotator 为 None 或抛错
    orig_open_ann = flow.open_annotator
    flow.open_annotator = None
    flow.trigger_annotate()
    bad_sel_ann_none = selectors[-1]
    bad_sel_ann_none.on_selected((10, 20, 120, 80))
    status.drain()
    assert bad_sel_ann_none.closed_silent is True
    assert flow._overlay_opening is False and flow.overlay_active() is False
    assert "未配置标注视图" in posted[-1]["message"]

    flow.open_annotator = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("ann boom"))
    flow.trigger_annotate()
    bad_sel_ann_err = selectors[-1]
    bad_sel_ann_err.on_selected((10, 20, 120, 80))
    status.drain()
    assert bad_sel_ann_err.closed_silent is True
    assert flow._overlay_opening is False and flow.overlay_active() is False
    assert "标注浮窗无法打开：ann boom" == posted[-1]["message"]
    flow.open_annotator = orig_open_ann

    # 5. _prepare_ui（get_virtual_rect 抛错）与后台 invoke_ui(_open_selector_ui) 抛错均复位状态并上报 error
    orig_vrect = flow._get_virtual_rect
    flow._get_virtual_rect = lambda: (_ for _ in ()).throw(RuntimeError("screen gone"))
    flow.trigger_ocr()
    status.drain()
    assert flow._capturing is False and flow.overlay_active() is False
    assert "截屏失败：screen gone" == posted[-1]["message"]
    flow._get_virtual_rect = orig_vrect

    invoke_count = {"n": 0}
    orig_invoke = flow.invoke_ui

    def fail_second_invoke(fn):
        invoke_count["n"] += 1
        if invoke_count["n"] == 2:
            raise RuntimeError("bridge closed")
        return orig_invoke(fn)

    flow.invoke_ui = fail_second_invoke
    flow.trigger_ocr()
    status.drain()
    assert flow._selector_opening is False and flow.overlay_active() is False
    assert "框选器无法打开：bridge closed" == posted[-1]["message"]
    flow.invoke_ui = orig_invoke


def test_ui_bridge_configure_qt_platform_env_and_open_frozen_selector(monkeypatch):
    """P2-4：覆盖 configure_qt_platform_env、open_frozen_selector 与 open_translate_overlay / open_annotator 的单屏/多屏与 repaint 契约。"""
    import os
    import sys

    from _desktop import ui_bridge as ui_bridge_mod
    from _desktop.ui_bridge import configure_qt_platform_env, open_annotator, open_frozen_selector, open_translate_overlay

    assert not hasattr(ui_bridge_mod, "open_live_selector")

    # 1. Linux + DISPLAY 且未显式指定 QT_QPA_PLATFORM → 自动设为 xcb
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    configure_qt_platform_env()
    assert os.environ.get("QT_QPA_PLATFORM") == "xcb"

    # 2. 用户已显式指定 QT_QPA_PLATFORM → 保持原值
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    configure_qt_platform_env()
    assert os.environ.get("QT_QPA_PLATFORM") == "wayland"

    # 3. 无 DISPLAY → 不强制写 xcb
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    configure_qt_platform_env()
    assert "QT_QPA_PLATFORM" not in os.environ

    # 4. 非 Linux 平台 → 不干预
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    configure_qt_platform_env()
    assert "QT_QPA_PLATFORM" not in os.environ

    # 5. open_frozen_selector：透传 frozen_png；单屏 showFullScreen、多屏 show，且顺序严格为 show* -> raise_ -> activateWindow -> repaint
    calls: list[str] = []
    seen_kwargs: list[dict] = []

    class DummySelector:
        def __init__(self, **kw) -> None:
            seen_kwargs.append(kw)

        def showFullScreen(self) -> None:
            calls.append("showFullScreen")

        def show(self) -> None:
            calls.append("show")

        def raise_(self) -> None:
            calls.append("raise_")

        def activateWindow(self) -> None:
            calls.append("activateWindow")

        def repaint(self) -> None:
            calls.append("repaint")

    single_app = type("SingleApp", (), {"screens": lambda self: [object()]})()
    on_sel = lambda _rect: None
    on_cancel = lambda: None
    sel1 = open_frozen_selector(single_app, DummySelector, b"frozen-png", (0, 0, 1920, 1080), "截屏原位翻译", on_sel, on_cancel)
    assert isinstance(sel1, DummySelector)
    assert seen_kwargs[-1] == {
        "frozen_png": b"frozen-png",
        "virtual_geometry": (0, 0, 1920, 1080),
        "mode_label": "截屏原位翻译",
        "on_selected": on_sel,
        "on_cancelled": on_cancel,
    }
    assert calls == ["showFullScreen", "raise_", "activateWindow", "repaint"]

    calls.clear()
    multi_app = type("MultiApp", (), {"screens": lambda self: [object(), object()]})()
    open_frozen_selector(multi_app, DummySelector, b"frozen-png-2", (-1920, 0, 3840, 1080), "纯 OCR 识别", on_sel, on_cancel)
    assert seen_kwargs[-1]["frozen_png"] == b"frozen-png-2"
    assert calls == ["show", "raise_", "activateWindow", "repaint"]

    # 6. open_translate_overlay：全屏透明浮窗与框选器同一显示方式；模式、虚拟桌面几何与回调按契约 D 透传并同步 repaint
    class DummyOverlay(DummySelector):
        def __init__(self, png, rect, **kw) -> None:
            seen_kwargs.append({"png": png, "rect": rect, **kw})

    callbacks = {"on_copy_text": lambda _t: True, "on_retry": lambda: None, "on_closed": lambda: None}
    for app_, expected in ((single_app, "showFullScreen"), (multi_app, "show")):
        calls.clear()
        ov = open_translate_overlay(app_, DummyOverlay, b"png", (10, 20, 30, 40), mode="ocr",
                                    virtual_rect=(0, 0, 1920, 1080), **callbacks)
        assert isinstance(ov, DummyOverlay) and calls == [expected, "raise_", "activateWindow", "repaint"]
        assert seen_kwargs[-1] == {"png": b"png", "rect": (10, 20, 30, 40), "mode": "ocr",
                                   "virtual_geometry": (0, 0, 1920, 1080), **callbacks}

    # 7. open_annotator：与原位浮窗同一显示方式，必传并透传 initial_style / on_style_changed / 回调与虚拟桌面几何并同步 repaint
    ann_cbs = {
        "initial_style": ("#2f81f7", "thick"),
        "on_style_changed": lambda _s: None,
        "on_copy_image": lambda _b: True,
        "on_save_image": lambda _b: SaveResult(path="/pictures/x.png"),
        "on_closed": lambda: None,
    }
    for app_, expected in ((single_app, "showFullScreen"), (multi_app, "show")):
        calls.clear()
        ann = open_annotator(app_, DummyOverlay, b"png", (15, 25, 35, 45), virtual_rect=(0, 0, 1920, 1080), **ann_cbs)
        assert isinstance(ann, DummyOverlay) and calls == [expected, "raise_", "activateWindow", "repaint"]
        assert seen_kwargs[-1] == {
            "png": b"png",
            "rect": (15, 25, 35, 45),
            "virtual_geometry": (0, 0, 1920, 1080),
            **ann_cbs,
        }
    for missing_key in ("initial_style", "on_style_changed"):
        incomplete = {k: v for k, v in ann_cbs.items() if k != missing_key}
        with pytest.raises(TypeError):
            open_annotator(single_app, DummyOverlay, b"png", (15, 25, 35, 45), virtual_rect=(0, 0, 1920, 1080), **incomplete)


def test_capture_flow_annotate_mode_exclusion_copy_and_save(tmp_path: Path):
    """U11：标注模式共用框选器与 generation 互斥、复制成败不发 status、保存与重名递增及失败返回原因。"""

    class GuardApi:
        def __init__(self) -> None:
            self.ocr_calls = 0

        def get_config(self) -> dict:
            return {"translate_lang_pair": "zh-en"}

        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            self.ocr_calls += 1
            return _ocr_payload("译文", mode=mode, lang_pair=lang_pair)

    copy_outcomes = iter([None, "xclip 不可用"])

    def fake_copy_image(png: bytes) -> str | None:
        copied_pngs.append(png)
        outcome = next(copy_outcomes, "boom")
        if outcome == "boom":
            raise RuntimeError("clipboard crashed")
        return outcome

    copied_pngs: list[bytes] = []
    fixed_time = datetime(2026, 11, 21, 10, 20, 30)
    save_root = tmp_path / "Pictures" / "Ollama Voice Input"
    api = GuardApi()
    flow, status, posted, selectors, overlays, _copied = _make_capture_harness(
        api,
        copy_image_fn=fake_copy_image,
        save_dir_fn=lambda: save_root,
        now_fn=lambda: fixed_time,
    )
    annotators: list[FakeAnnotator] = flow._test_annotators  # type: ignore[attr-defined]

    # 1. 触发标注 → 复用红十字框选器（标签「截屏标注」），选区后打开标注浮窗且不调 OCR API
    flow.trigger_annotate()
    assert len(selectors) == 1 and selectors[0].mode_label == "截屏标注"
    assert flow.overlay_active() is True
    selectors[0].on_selected((20, 30, 400, 300))
    assert len(annotators) == 1 and overlays == [] and api.ocr_calls == 0
    ann = annotators[0]
    assert ann.png.startswith(b"\x89PNG") and ann.rect == (20, 30, 400, 300) and ann.virtual_rect == (0, 0, 1920, 1080)
    assert selectors[0].closed_silent is True and ann.raised == 1 and ann.activated == 1

    # 2. 复制图片：成功返回 True、失败/异常返回 False，全过程不发 status 事件
    assert [ann.on_copy_image(b"annotated-png") for _ in range(3)] == [True, False, False]
    assert copied_pngs == [b"annotated-png"] * 3
    status.drain()
    assert posted == []

    # 3. 保存图片：首次写入 `截屏 YYYY-MM-DD HH-MM-SS.png`，同秒重名依次加 ` (2)`、` (3)`
    r1 = ann.on_save_image(b"png-1")
    r2 = ann.on_save_image(b"png-2")
    r3 = ann.on_save_image(b"png-3")
    assert r1 == SaveResult(path=str(save_root / "截屏 2026-11-21 10-20-30.png"))
    assert r2 == SaveResult(path=str(save_root / "截屏 2026-11-21 10-20-30 (2).png"))
    assert r3 == SaveResult(path=str(save_root / "截屏 2026-11-21 10-20-30 (3).png"))
    assert Path(r1.path).read_bytes() == b"png-1"  # type: ignore[arg-type]
    assert Path(r2.path).read_bytes() == b"png-2"  # type: ignore[arg-type]
    assert Path(r3.path).read_bytes() == b"png-3"  # type: ignore[arg-type]

    # 4. 保存失败（目标路径被文件占用触发 OSError）→ 返回 SaveResult(error=...) 不抛异常
    occupied = tmp_path / "file_as_dir"
    occupied.write_text("x", encoding="utf-8")
    flow.save_dir = lambda: occupied
    fail_res = ann.on_save_image(b"png-fail")
    assert fail_res.path is None and fail_res.error

    # SaveResult 契约不变量：path 与 error 必须恰好一个非空
    with pytest.raises(ValueError):
        SaveResult()
    with pytest.raises(ValueError):
        SaveResult(path="/a.png", error="err")

    # 5. generation 互斥：标注打开时触发翻译 → 静默关闭旧标注；翻译打开时触发标注 → 静默关闭旧原位浮窗
    flow.trigger_translate()
    assert ann.closed_silent is True and len(selectors) == 2
    selectors[-1].on_selected((10, 10, 80, 40))
    assert len(overlays) == 1 and api.ocr_calls == 1
    flow.trigger_annotate()
    assert overlays[0].closed_silent is True and len(selectors) == 3
    selectors[-1].on_selected((10, 10, 80, 40))
    assert len(annotators) == 2
    annotators[-1].close()
    assert flow.overlay_active() is False


def test_capture_flow_handle_escape_unified_routing_and_close_overlay_separation():
    """U11（10.15.5）：handle_escape 统一路由（框选器→取消；原位浮窗/标注→handle_escape；连续两次不去重；无活动窗→False），
    且 close_overlay 仅走 close_silent 不再接 Esc。"""

    class Api:
        def ocr_inplace(self, png: bytes, *, mode: str = "translate", lang_pair: str | None = None, lang=None) -> dict:
            return _ocr_payload("OCR", mode=mode, lang_pair=lang_pair)

    flow, _status, _posted, selectors, overlays, _copied = _make_capture_harness(Api(), two_step_escape=True)
    annotators: list[FakeAnnotator] = flow._test_annotators  # type: ignore[attr-defined]

    # 1. 无活动窗 → False
    assert flow.handle_escape() is False

    # 2. 框选器活动 → 取消框选并返回 True
    flow.trigger_annotate()
    assert flow.handle_escape() is True
    assert selectors[0].closed is True and flow.overlay_active() is False
    assert flow.handle_escape() is False

    # 3. 原位浮窗活动 → 路由到 overlay.handle_escape（绝不走 close_silent）
    flow.trigger_ocr()
    selectors[-1].on_selected((0, 0, 50, 50))
    ov = overlays[0]
    assert flow.handle_escape() is True
    assert ov.escape_calls == 1 and ov.closed is True and ov.closed_silent is False
    assert flow.overlay_active() is False

    # 4. 标注浮窗活动（两段式 Esc）：连续两次 handle_escape 各路由一次（不去重），第 1 次仅退出输入、第 2 次关闭
    flow.trigger_annotate()
    selectors[-1].on_selected((0, 0, 80, 60))
    ann = annotators[0]
    assert ann.text_editing is True
    assert flow.handle_escape() is True
    assert ann.escape_calls == 1 and ann.text_editing is False and ann.closed is False
    assert flow.overlay_active() is True
    assert flow.handle_escape() is True
    assert ann.escape_calls == 2 and ann.closed is True and ann.closed_silent is False
    assert flow.overlay_active() is False
    assert flow.handle_escape() is False

    # 5. close_overlay 只用于退出/强制清理（走 close_silent，不调 handle_escape）
    flow.trigger_annotate()
    selectors[-1].on_selected((0, 0, 80, 60))
    ann2 = annotators[1]
    flow.close_overlay()
    assert ann2.closed_silent is True and ann2.escape_calls == 0 and flow.overlay_active() is False


def test_capture_flow_annotate_style_memory_per_session_and_generation_isolation():
    """单次运行样式记忆：首开 None、实时写回、复制/Esc/取代/强关延续、返回前同步回调与过期代次隔离、非法值抛 ValueError、双实例隔离。"""
    flow, _status, _posted, selectors, _overlays, _copied = _make_capture_harness(
        api=object(),
        copy_image_fn=lambda _png: None,
    )
    annotators: list[FakeAnnotator] = flow._test_annotators  # type: ignore[attr-defined]

    # 1. 首开前与首次打开：initial_style 为 None（默认色/档仅存 _ui_desktop/theme.py 单源）
    assert flow.annotate_style is None
    flow.trigger_annotate()
    selectors[-1].on_selected((10, 20, 300, 200))
    ann1 = annotators[-1]
    assert ann1.initial_style is None

    # 2. 实时写回 + 复制关闭后再次打开沿用上次样式
    ann1.on_style_changed(("#2f81f7", "thick"))
    assert flow.annotate_style == ("#2f81f7", "thick")
    assert ann1.on_copy_image(b"png") is True
    ann1.close()
    assert flow.annotate_style == ("#2f81f7", "thick")

    flow.trigger_annotate()
    selectors[-1].on_selected((10, 20, 300, 200))
    ann2 = annotators[-1]
    assert ann2.initial_style == ("#2f81f7", "thick")

    # 3. Esc 取消后再次打开仍沿用上次所选样式
    ann2.on_style_changed(("#3fb950", "thin"))
    assert flow.handle_escape() is True
    assert ann2.closed is True and flow.annotate_style == ("#3fb950", "thin")

    flow.trigger_annotate()
    selectors[-1].on_selected((10, 20, 300, 200))
    ann3 = annotators[-1]
    assert ann3.initial_style == ("#3fb950", "thin")

    # 4. 被新一次截屏取代（close_silent）以及 close_overlay 强制清理均不清空记忆
    ann3.on_style_changed(("#f59e0b", "medium"))
    flow.trigger_annotate()
    assert ann3.closed_silent is True
    selectors[-1].on_selected((10, 20, 300, 200))
    ann4 = annotators[-1]
    assert ann4.initial_style == ("#f59e0b", "medium")

    flow.close_overlay()
    assert ann4.closed_silent is True and flow.annotate_style == ("#f59e0b", "medium")
    flow.trigger_annotate()
    selectors[-1].on_selected((10, 20, 300, 200))
    ann5 = annotators[-1]
    assert ann5.initial_style == ("#f59e0b", "medium")

    # 5. 过期代次回调（已关闭或被取代的旧浮窗）不得污染当前记忆
    ann1.on_style_changed(("#111827", "thick"))
    ann3.on_style_changed(("#111827", "thick"))
    ann4.on_style_changed(("#111827", "thick"))
    assert flow.annotate_style == ("#f59e0b", "medium")

    # 6. 非法或空白样式立即抛 ValueError，且不篡改既有记忆
    for bad in (
        None,
        "#ff2d2d",
        ("#ff2d2d",),
        ("#ff2d2d", "medium", "extra"),
        ["#ff2d2d", "medium"],
        ("", "medium"),
        ("   ", "medium"),
        ("#ff2d2d", ""),
        ("#ff2d2d", " \t "),
        (123, "medium"),
    ):
        with pytest.raises(ValueError, match="AnnotateStyle"):
            ann5.on_style_changed(bad)  # type: ignore[arg-type]
    assert flow.annotate_style == ("#f59e0b", "medium")

    # 7. open_annotator 返回前同步触发 on_style_changed（handle 尚未就位，None is None 陷阱）必须被拒写
    premature_flow, _st2, _p2, premature_sels, _o2, _c2 = _make_capture_harness(api=object())
    premature_anns: list[FakeAnnotator] = []

    def premature_open_annotator(png, rect, **kwargs):
        kwargs["on_style_changed"](("#111827", "thick"))
        ann = FakeAnnotator(png, rect, **kwargs)
        premature_anns.append(ann)
        return ann

    premature_flow.open_annotator = premature_open_annotator
    premature_flow.trigger_annotate()
    premature_sels[-1].on_selected((0, 0, 100, 80))
    assert premature_anns[0].initial_style is None
    assert premature_flow.annotate_style is None
    # 返回并登记句柄后的正常回调可写入
    premature_anns[0].on_style_changed(("#f8fafc", "thin"))
    assert premature_flow.annotate_style == ("#f8fafc", "thin")

    # 8. 双独立 CaptureFlow 实例零共享（杜绝模块级可变全局状态）
    fresh_flow, _st3, _p3, fresh_sels, _o3, _c3 = _make_capture_harness(api=object())
    fresh_anns: list[FakeAnnotator] = fresh_flow._test_annotators  # type: ignore[attr-defined]
    assert fresh_flow.annotate_style is None
    fresh_flow.trigger_annotate()
    fresh_sels[-1].on_selected((0, 0, 120, 90))
    assert fresh_anns[0].initial_style is None
    assert flow.annotate_style == ("#f59e0b", "medium")


def test_capture_flow_annotate_real_save_and_linux_xclip_roundtrip(tmp_path: Path):
    """R3 真实链路（业务半程）：真实 PNG 保存到临时图片目录 + 有 DISPLAY 且有 xclip 时真实写入并读回 image/png 校验字节一致。"""
    import io
    import os
    import shutil
    from PIL import Image

    from _linux.clipboard import SystemClipboard
    from _paths import APP_DISPLAY_NAME
    from _platform.linux import LinuxPlatformAdapter

    buf = io.BytesIO()
    Image.new("RGB", (24, 16), (220, 48, 48)).save(buf, format="PNG")
    real_png = buf.getvalue()

    save_root = tmp_path / "Pictures" / APP_DISPLAY_NAME
    adapter = LinuxPlatformAdapter(environ=os.environ, uinput_factory=None)
    flow, status, posted, selectors, _overlays, _copied = _make_capture_harness(
        api=object(),
        capture_fn=lambda: ScreenCapture(png_bytes=real_png, virtual_rect=(0, 0, 24, 16)),
        get_virtual_rect_fn=lambda: (0, 0, 24, 16),
        copy_image_fn=adapter.write_clipboard_png,
        save_dir_fn=lambda: save_root,
        now_fn=lambda: datetime(2026, 11, 21, 12, 0, 0),
    )
    annotators: list[FakeAnnotator] = flow._test_annotators  # type: ignore[attr-defined]
    flow.trigger_annotate()
    selectors[0].on_selected((0, 0, 24, 16))
    ann = annotators[0]

    # 1. 真实落盘保存
    saved = ann.on_save_image(real_png)
    assert saved.error is None and saved.path is not None
    saved_file = Path(saved.path)
    assert saved_file == save_root / "截屏 2026-11-21 12-00-00.png"
    assert saved_file.read_bytes() == real_png

    # 2. 真实 Linux xclip 写入 + 读回（保留并恢复宿主原剪贴板）
    if not (os.environ.get("DISPLAY") and shutil.which("xclip")):
        pytest.skip("无 DISPLAY 或 xclip，跳过真实 Linux 剪贴板读回")
    clip = SystemClipboard()
    prev = clip.snapshot()
    try:
        assert ann.on_copy_image(real_png) is True
        readback = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout
        assert readback == real_png
    finally:
        clip.restore_snapshot(prev)
    status.drain()
    assert posted == []


def test_attach_app_log_writes_rotates_and_restores_streams(tmp_path: Path, monkeypatch):
    """N1：主进程日志经 attach_app_log 复用 rotate_log（>10MB 轮转为 .1），双写 stdout/stderr 且关闭后恢复原流。"""
    import io
    import sys

    from _platform.child_process import LOG_ROTATE_BYTES, attach_app_log, rotate_log

    log_path = tmp_path / "logs" / "app.log"
    log_path.parent.mkdir(parents=True)
    with open(log_path, "wb") as fh:
        fh.truncate(LOG_ROTATE_BYTES + 1)

    fake_out = io.StringIO()
    fake_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    handle = attach_app_log(log_path)
    try:
        rotated = log_path.with_name("app.log.1")
        assert rotated.is_file() and rotated.stat().st_size == LOG_ROTATE_BYTES + 1
        print("app-started-line", flush=True)
        print("app-error-line", file=sys.stderr, flush=True)
    finally:
        handle.close()
        handle.close()  # 幂等

    assert sys.stdout is fake_out and sys.stderr is fake_err
    assert "app-started-line" in fake_out.getvalue()
    assert "app-error-line" in fake_err.getvalue()
    persisted = log_path.read_text(encoding="utf-8")
    assert "app-started-line" in persisted and "app-error-line" in persisted

    # pythonw 等无终端场景（原 stdout 为 None）仍正常写入日志文件
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    handle2 = attach_app_log(log_path)
    try:
        print("headless-line", flush=True)
    finally:
        handle2.close()
    assert sys.stdout is None and sys.stderr is None
    assert "headless-line" in log_path.read_text(encoding="utf-8")

    # 公开 rotate_log 小于阈值时不轮转
    small = tmp_path / "logs" / "small.log"
    small.write_text("hi", encoding="utf-8")
    rotate_log(small)
    assert small.is_file() and not small.with_name("small.log.1").exists()


# ------------------------------------------------------------------ VoiceFlow


def test_voice_mic_failure_visible_and_returns_false():
    status, posted = _status()

    class Broken:
        def start(self, sink):
            raise OSError("PortAudio 设备不可用")

        def stop(self):
            pass

    calls: list[str] = []

    class Api:
        def create_session(self, **kw):
            calls.append("create")
            return "s"

    flow = VoiceFlow(api=Api(), status=status, probe=StaticFocusProbe(1), source_factory=Broken)
    assert flow.start() is False
    status.drain()
    assert posted[-1]["event"] == "error" and "麦克风" in posted[-1]["message"]
    assert calls == []  # 未开麦就不建会话


def test_voice_session_create_failure_visible():
    status, posted = _status()

    class Api:
        def create_session(self, **kw):
            raise RuntimeError("ASR sidecar 未就绪")

    flow = VoiceFlow(
        api=Api(),
        status=status,
        probe=StaticFocusProbe(1),
        source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000),
    )
    assert flow.start() is True
    flow.stop()
    flow.last_upload.join(5)
    status.drain()
    assert flow.last_upload.outcome == "failed"
    assert posted[-1]["error"] == ErrorCode.asr_unavailable.value


def test_voice_flow_propagates_api_error_codes():
    """P2-3 回归：VoiceFlow 在 create/finish/submit 收到 ApiError 时透传合法 ErrorCode，未知码回退 asr_unavailable。"""
    cases = [
        ("create", ApiError(409, ErrorCode.conflict.value, "会话忙"), ErrorCode.conflict.value),
        ("finish", ApiError(504, ErrorCode.timeout.value, "识别超时"), ErrorCode.timeout.value),
        ("submit", ApiError(503, ErrorCode.polish_failed.value, "Ollama 模型未下载"), ErrorCode.polish_failed.value),
        ("submit", ApiError(500, "unknown_future_code", "未知错误"), ErrorCode.asr_unavailable.value),
    ]
    for stage, exc, expected_code in cases:
        status, posted = _status()

        class StageFailApi:
            def create_session(self, **kw) -> str:
                del kw
                if stage == "create":
                    raise exc
                return "s-err"

            def upload_chunk(self, sid: str, idx: int, pcm: bytes) -> None:
                del sid, idx, pcm

            def finish_session(self, sid: str) -> dict:
                del sid
                if stage == "finish":
                    raise exc
                return {"event": "closed"}

            def cancel_session(self, sid: str) -> dict:
                del sid
                return {"event": "closed"}

            def submit_utterance(self, **kw) -> dict:
                del kw
                if stage == "submit":
                    raise exc
                return {"ok": True}

        flow = VoiceFlow(
            api=StageFailApi(),
            status=status,
            probe=StaticFocusProbe(1),
            source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000),
        )
        assert flow.start() is True
        flow.stop()
        flow.last_upload.join(5)
        status.drain()
        assert flow.last_upload.outcome == "failed"
        assert posted[-1]["event"] == "error"
        assert posted[-1]["error"] == expected_code, (stage, posted[-1])
        assert exc.message in posted[-1]["message"]


def test_voice_passes_hotword_context_with_ttl_cache():
    status, _posted = _status()
    now = [100.0]

    class Api:
        def __init__(self) -> None:
            self.config_reads = 0
            self.contexts: list[str | None] = []

        def get_config(self) -> dict:
            self.config_reads += 1
            return {"custom_keywords": ["PySide6", "Qwen3-ASR", "PySide6", "  "]}

        def create_session(self, *, language=None, context=None) -> str:
            self.contexts.append(context)
            return f"s-{len(self.contexts)}"

        def upload_chunk(self, sid, idx, pcm) -> None:
            pass

        def finish_session(self, sid) -> dict:
            return {"event": "closed"}

        def submit_utterance(self, **kw) -> dict:
            return {"ok": True}

    api = Api()
    provider = build_keywords_context_provider(api, ttl_s=10.0, clock=lambda: now[0])
    flow = VoiceFlow(
        api=api,
        status=status,
        probe=StaticFocusProbe(1),
        source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000),
        context_provider=provider,
    )
    for _ in range(2):
        assert flow.start() is True
        flow.stop()
        flow.last_upload.join(5)
        assert flow.last_upload.outcome == "submitted"
    assert api.contexts == ["PySide6, Qwen3-ASR", "PySide6, Qwen3-ASR"]
    assert api.config_reads == 1  # TTL 内命中缓存

    now[0] += 11.0
    assert provider() == "PySide6, Qwen3-ASR"
    assert api.config_reads == 2


def test_voice_rapid_back_to_back_serializes_sessions_without_409():
    """第 1 句 finish 尚未返回时立刻按下第 2 句：本地立即录第 2 句，后台严格等待第 1 句 finish 后才 create 第 2 句。"""
    status, posted = _status()
    first_finish_entered = threading.Event()
    allow_first_finish = threading.Event()
    timeline: list[str] = []
    lock = threading.Lock()
    active_sessions = {"count": 0}

    class SerialApi:
        def create_session(self, *, language=None, context=None) -> str:
            del language, context
            with lock:
                if active_sessions["count"] > 0:
                    raise ApiError(409, ErrorCode.conflict.value, "sidecar busy 409")
                active_sessions["count"] += 1
                sid = f"s{len([t for t in timeline if t.startswith('create:')]) + 1}"
                timeline.append(f"create:{sid}")
                return sid

        def upload_chunk(self, sid: str, idx: int, pcm: bytes) -> None:
            del pcm
            with lock:
                timeline.append(f"chunk:{sid}:{idx}")

        def finish_session(self, sid: str) -> dict:
            if sid == "s1":
                first_finish_entered.set()
                assert allow_first_finish.wait(5.0)
            with lock:
                active_sessions["count"] -= 1
                timeline.append(f"finish:{sid}")
            return {"event": "closed"}

        def cancel_session(self, sid: str) -> dict:
            with lock:
                active_sessions["count"] = max(0, active_sessions["count"] - 1)
                timeline.append(f"cancel:{sid}")
            return {"event": "closed"}

        def submit_utterance(self, *, job_id: str, session_id: str, focus_id: str) -> dict:
            with lock:
                timeline.append(f"submit:{session_id}:{focus_id}")
            return {"ok": True}

    flow = VoiceFlow(
        api=SerialApi(),
        status=status,
        probe=StaticFocusProbe(11),
        source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000),
    )

    # 第 1 句开始并松手，后台卡在 finish_session(s1)
    assert flow.start() is True
    upload1 = flow.last_upload
    flow.stop()
    assert first_finish_entered.wait(5.0)

    # 立刻按下第 2 句并松手：麦克风立即完成本地录音，但后台上传必须等 s1 finish 完
    assert flow.start() is True
    upload2 = flow.last_upload
    flow.stop()
    time.sleep(0.05)
    with lock:
        assert "create:s2" not in timeline, f"第 2 句提前抢占 sidecar: {timeline}"

    allow_first_finish.set()
    upload1.join(5.0)
    upload2.join(5.0)
    status.drain()

    assert upload1.outcome == "submitted" and upload2.outcome == "submitted"
    assert posted == []  # 零 409 报错
    assert timeline.index("finish:s1") < timeline.index("create:s2")
    assert timeline.index("create:s2") < timeline.index("finish:s2")


def test_voice_stop_if_exceeded_finishes_recording():
    now = [100.0]
    status, _ = _status()

    class Api:
        def __init__(self) -> None:
            self.finished: list[str] = []

        def create_session(self, **kw) -> str:
            return "s-long"

        def upload_chunk(self, sid, idx, pcm) -> None:
            pass

        def finish_session(self, sid) -> dict:
            self.finished.append(sid)
            return {"event": "closed"}

        def submit_utterance(self, **kw) -> dict:
            return {"ok": True}

    api = Api()
    flow = VoiceFlow(
        api=api,
        status=status,
        probe=StaticFocusProbe(5),
        source_factory=lambda: FakeAudioSource(b"\x01\x00" * 16000),
        clock=lambda: now[0],
    )
    assert flow.start() is True
    assert flow.stop_if_exceeded(120.0) is False
    now[0] += 121.0
    assert flow.stop_if_exceeded(120.0) is True
    flow.last_upload.join(5.0)
    assert api.finished == ["s-long"] and flow.last_upload.outcome == "submitted"


def test_voice_finish_and_cancel_failure_do_not_hang_upload_thread():
    """P0-1 回归：finish_session / cancel_session 抛异常时上传线程立即退出，不永久挂死也不阻塞下一句。"""
    status, posted = _status()
    state = {
        "fail_finish": True,
        "fail_cancel": False,
        "created": 0,
        "cancel_calls": 0,
    }

    class FlakyApi:
        def create_session(self, **kw) -> str:
            del kw
            state["created"] += 1
            return f"s-{state['created']}"

        def upload_chunk(self, sid: str, idx: int, pcm: bytes) -> None:
            del sid, idx, pcm

        def finish_session(self, sid: str) -> dict:
            if state["fail_finish"]:
                raise RuntimeError(f"finish boom {sid}")
            return {"event": "closed"}

        def cancel_session(self, sid: str) -> dict:
            state["cancel_calls"] += 1
            if state["fail_cancel"]:
                raise RuntimeError(f"cancel boom {sid}")
            return {"event": "closed"}

        def submit_utterance(self, **kw) -> dict:
            del kw
            return {"ok": True}

    sources = [
        FakeAudioSource(b"\x01\x00" * 16000),  # 1. finish 抛异常
        FakeAudioSource(b"\x01\x00" * 16000),  # 2. 紧随其后恢复正常
        FakeAudioSource(b"\x00\x00" * 1600),   # 3. 过短 -> cancel 抛异常
        FakeAudioSource(b"\x01\x00" * 16000),  # 4. 紧随其后恢复正常
    ]
    it = iter(sources)
    flow = VoiceFlow(
        api=FlakyApi(),
        status=status,
        probe=StaticFocusProbe(9),
        source_factory=lambda: next(it),
    )

    # 1. finish_session 抛异常 -> 线程必须在 2s 内退出，不可永久阻塞在 _drain_until_end
    assert flow.start() is True
    upload1 = flow.last_upload
    flow.stop()
    upload1.join(timeout=2.0)
    assert not upload1._thread.is_alive()
    assert upload1.outcome == "failed"

    # 2. 后续新一轮录音能立即创建会话并提交
    state["fail_finish"] = False
    assert flow.start() is True
    upload2 = flow.last_upload
    flow.stop()
    upload2.join(timeout=2.0)
    assert not upload2._thread.is_alive()
    assert upload2.outcome == "submitted"

    # 3. cancel_session 抛异常 -> 线程立即退出，不重复调 cancel_session
    state["fail_cancel"] = True
    cancels_before = state["cancel_calls"]
    assert flow.start() is True
    upload3 = flow.last_upload
    flow.stop()
    upload3.join(timeout=2.0)
    assert not upload3._thread.is_alive()
    assert upload3.outcome == "failed"
    assert state["cancel_calls"] == cancels_before + 1

    # 4. 后续新一轮录音仍可正常创建并提交
    state["fail_cancel"] = False
    assert flow.start() is True
    upload4 = flow.last_upload
    flow.stop()
    upload4.join(timeout=2.0)
    assert not upload4._thread.is_alive()
    assert upload4.outcome == "submitted"

    status.drain()
    assert sum(1 for p in posted if p["event"] == "error") == 2


def test_product_event_loop_request_stop_before_stream_close_exits_immediately():
    """P2-6 回归：先 request_stop() 再关闭阻塞中的 SSE 流，stop(timeout=0.5) 秒退不白等 0.5s。"""
    stream_entered = threading.Event()
    stream_closed = threading.Event()
    waits: list[float] = []

    def blocking_iter(_stop):
        stream_entered.set()
        stream_closed.wait(5.0)
        raise ConnectionResetError("closed by client")
        yield {}  # pragma: no cover

    loop = ProductEventLoop(
        iter_events=blocking_iter,
        on_event=lambda _ev: None,
        wait_fn=lambda d: (waits.append(d) or True),
    )
    loop.start()
    assert stream_entered.wait(2.0)

    t0 = time.monotonic()
    loop.request_stop()
    stream_closed.set()
    loop.stop(timeout=0.5)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2, f"退出耗时异常偏长: {elapsed:.3f}s"
    assert waits == []


def test_hotkey_rig_reset_voice_recovers_after_watchdog_timeout():
    """P1-4 回归：120s 看门狗触发后调用 hotkey_rig.reset_voice()，下次按语音热键不吞键。"""
    from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyContext
    from _linux.hotkey_manager import HotkeyManager
    from _linux.inject_guard import InjectGuard
    from _platform._pynput_hotkeys import PynputHotkeyRig
    from _platform.linux import LinuxHotkeyRig
    from _platform.protocol import HotkeyRig

    # 1. PynputHotkeyRig（Windows / macOS 共享实现）
    events: list[str] = []

    def parse_simple(hk: str) -> tuple[int, int]:
        parts = hk.lower().split("+")
        mask = 0
        for p in parts[:-1]:
            mask |= {"ctrl": 1, "alt": 2, "shift": 4, "super": 8}[p]
        return mask, 27 if parts[-1] == "escape" else ord(parts[-1])  # 监听器构造时解析 Esc 键码

    def extract_simple(key: tuple[int, int]) -> tuple[int, int]:
        return key

    rig = PynputHotkeyRig(
        DEFAULTS,
        backend_name="test-pynput",
        hotkey_parser=parse_simple,
        key_extractor=extract_simple,
        on_voice_start=lambda: (events.append("start") or True),
        on_voice_stop=lambda: events.append("stop"),
        on_screenshot=lambda: None,
    )
    assert isinstance(rig, HotkeyRig)

    # 模拟按下 Ctrl+Alt+Z 后松开事件丢失
    rig.handle_press((1, -1))
    rig.handle_press((2, -1))
    rig.handle_press((0, ord("z")))
    assert rig.voice_active is True and events == ["start"]

    # 看门狗超时强制停止语音后调用 reset_voice()
    rig.reset_voice()
    assert rig.voice_active is False

    # 无中间 release 直接再次按 Ctrl+Alt+Z，不得被吞掉
    rig.handle_press((1, -1))
    rig.handle_press((2, -1))
    rig.handle_press((0, ord("z")))
    assert rig.voice_active is True and events == ["start", "start"]

    # 2. LinuxHotkeyRig + HotkeyManager：唯一按住状态由分发器持有，复位不重复调用 voice_stop
    linux_starts: list[str] = []
    released_hotkeys: list[str] = []

    class FakeJanitor:
        evdev_any_modifier_down = None

        def release_hotkey(self, hk: str) -> None:
            released_hotkeys.append(hk)

    ctx = HotkeyContext(
        actions=CallbackHotkeyActions(
            on_voice_start=lambda: (linux_starts.append("start") or True),
            on_voice_stop=lambda: linux_starts.append("stop"),
            on_screenshot=lambda: None,
        ),
        bindings=DEFAULTS,
        janitor=FakeJanitor(),
        guard=InjectGuard(),
        session_type="x11",
    )
    mgr = HotkeyManager(ctx, on_error=lambda _reason: None)
    linux_rig = LinuxHotkeyRig(mgr)
    assert isinstance(linux_rig, HotkeyRig)

    # 一次按下开始录音，随后结束事件丢失（看门狗 120s 到时强制 finish）
    mgr.dispatcher.interval_started("voice", 1000)
    mgr.dispatcher.activated("voice", 1000)
    assert mgr.dispatcher.voice_active is True and linux_starts == ["start"]

    linux_rig.reset_voice()
    assert mgr.dispatcher.voice_active is False
    assert released_hotkeys == [DEFAULTS.voice]
    assert linux_starts == ["start"]  # reset_voice 不重复调用 voice_stop

    # 再次按下可立即触发下一句录音
    mgr.dispatcher.interval_started("voice", 9000)
    mgr.dispatcher.activated("voice", 9000)
    assert mgr.dispatcher.voice_active is True and linux_starts == ["start", "start"]


# ------------------------------------------------------------------ Actions / Repeat / SSE / Lock


def test_desktop_actions_four_key_dispatch():
    log: list[str] = []

    class V:
        def start(self):
            log.append("vstart")
            return True

        def stop(self):
            log.append("vstop")

    class C:
        def trigger_translate(self):
            log.append("translate")

        def trigger_ocr(self):
            log.append("ocr")

        def trigger_annotate(self):
            log.append("annotate")

        def overlay_active(self):
            return True

        def handle_escape(self):
            log.append("escape")
            return True

        def close_overlay(self):
            log.append("close")
            return False

    class R:
        def run(self):
            log.append("repeat")

    actions = DesktopActions(voice=V(), capture=C(), repeat=R(), spawn=lambda fn: fn())
    assert actions.voice_start() is True
    actions.voice_stop()
    actions.screenshot()
    actions.ocr()
    actions.annotate()
    actions.repeat()
    assert actions.overlay_active() is True
    assert actions.close_overlay() is True
    assert log == ["vstart", "vstop", "translate", "ocr", "annotate", "repeat", "escape"]


def test_repeat_action_confirm_resend():
    status, _posted = _status()
    sent: list[dict] = []

    class Api:
        def resend(self, **kw):
            sent.append(kw)
            return {"ok": True}

    repeat = RepeatAction(
        api=Api(),
        last=LastDelivered(),
        probe=StaticFocusProbe(42),
        status=status,
        job_ids=lambda: "desk-resend-1",
    )
    assert repeat.confirm_resend("orig-1", "未确认句", confirm=False) is False
    assert sent == []
    assert repeat.confirm_resend("orig-1", "未确认句", confirm=True) is True
    assert sent == [
        {
            "job_id": "desk-resend-1",
            "text": "未确认句",
            "original_job_id": "orig-1",
            "focus_id": "42",
            "confirm": True,
        }
    ]


def test_hotkey_error_and_component_snapshot_are_resent_each_time_the_event_stream_connects():
    """快捷键组件失败原因与看护层组件快照留在 PlatformStatus；事件流每条连接收到首个事件（服务端 idle）后各重报一次。"""
    status, posted = _status()
    connections = {"n": 0}

    def iter_events(_stop):
        connections["n"] += 1
        if connections["n"] <= 2:
            yield {"event": "idle"}
            yield {"event": "recording"}
            return
        if connections["n"] == 3:
            status.set_hotkey_error(None)  # 期间恢复正常（例如 GNOME 改回可解析的绑定）
            yield {"event": "idle"}
            return
        raise ConnectionError("stop")

    waits: list[float] = []
    loop = ProductEventLoop(
        iter_events=iter_events,
        on_event=lambda _ev: None,
        on_connected=status.resend_on_connect,
        wait_fn=lambda delay: (waits.append(delay) or len(waits) >= 4),
    )
    status.set_hotkey_error("Wayland 下快捷键需要读取键盘设备（evdev），当前不可用：未找到可读的键盘设备")
    snapshot = [ComponentStatus(name=n, state="running", since="2026-11-20T00:00:00+00:00") for n in ("ollama", "asr", "server")]
    status.set_components(snapshot)
    loop.run_forever()
    status.drain()
    errors = [p for p in posted if p.get("event") == "error"]
    reports = [p for p in posted if "components" in p]
    # 立即 1 条 + 前两次连上各重发 1 条；恢复后第三次连上不再发
    assert len(errors) == 3 and {e["error"] for e in errors} == {"dependency_failed"}
    assert all("evdev" in e["message"] for e in errors)
    # 快照：变化时 1 次 + 三次连上各重报 1 次（服务端可能刚重启、缓存为空）
    assert len(reports) == 4 and all([c["name"] for c in r["components"]] == ["ollama", "asr", "server"] for r in reports)

    quiet, quiet_posted = _status()
    quiet.resend_on_connect()  # 从未出错、也没有快照：连上不发
    quiet.drain()
    assert quiet_posted == []


def test_component_command_and_config_changed_go_to_app_callbacks():
    commands: list[tuple[str, str]] = []
    reloads: list[bool] = []
    untouched = SimpleNamespace()  # 底栏与托盘没有任何方法：给应用自身的命令若去改它们会直接报错
    for ev in ({"event": "component_command", "action": "retry", "component": "server"}, {"event": "config_changed"}):
        dispatch_product_event(ev, hud=untouched, tray=untouched, on_component_command=lambda a, c: commands.append((a, c)),
                               on_config_changed=lambda: reloads.append(True))
    assert commands == [("retry", "server")] and reloads == [True]


def test_product_event_loop_exponential_backoff_and_hud_dispatch():
    waits: list[float] = []
    received: list[dict] = []
    attempt = {"n": 0}

    def iter_events(_stop):
        attempt["n"] += 1
        n = attempt["n"]
        if n in (1, 2, 3, 4, 5, 6):
            raise ConnectionError(f"down-{n}")
        if n == 7:
            yield {"event": "idle"}
            yield {"event": "recording"}
            return  # 服务端优雅断开后重连，退避必须重置为 1s
        raise ConnectionError("after-reset")

    def fake_wait(delay: float) -> bool:
        waits.append(delay)
        return len(waits) >= 7  # 6 次指数退避 + 1 次重置后退避

    loop = ProductEventLoop(
        iter_events=iter_events,
        on_event=received.append,
        backoff_initial_s=1.0,
        backoff_max_s=15.0,
        wait_fn=fake_wait,
    )
    loop.run_forever()
    assert waits == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 1.0]
    assert [e["event"] for e in received] == ["idle", "recording"]

    # 事件映射到 HUD 与托盘
    hud_log: list[tuple] = []
    tray_log: list[str] = []
    confirmed: list[tuple] = []

    class FakeHud:
        def set_idle(self): hud_log.append(("idle",))
        def set_recording(self): hud_log.append(("recording",))
        def set_partial(self, stable_text="", tail_text=""): hud_log.append(("partial", stable_text, tail_text))
        def set_polishing(self): hud_log.append(("polishing",))
        def set_typed(self, text=""): hud_log.append(("typed", text))
        def set_error(self, msg=""): hud_log.append(("error", msg))
        def show_toast(self, msg, kind="ok", duration_ms=1800): hud_log.append(("toast", msg, kind))
        def show_delivery_confirmation(self, jid, text, cb):
            hud_log.append(("confirm", jid, text))
            cb(jid, True)

    class FakeTray:
        def set_state(self, state): tray_log.append(state)

    hud, tray = FakeHud(), FakeTray()
    for ev in (
        {"event": "recording"},
        {"event": "partial", "stable_text": "你好", "tail_text": "世界"},
        {"event": "polishing"},
        {"event": "typed", "job_id": "j1", "text": "你好世界"},
        {"event": "error", "message": "测试错误"},
        {"event": "delivery_unknown", "job_id": "j2", "text": "重发我"},
        {"event": "idle"},
    ):
        dispatch_product_event(
            ev,
            hud=hud,
            tray=tray,
            on_confirm_resend=lambda jid, txt, ok: confirmed.append((jid, txt, ok)),
        )
    assert hud_log == [
        ("recording",),
        ("partial", "你好", "世界"),
        ("polishing",),
        ("typed", "你好世界"),
        ("error", "测试错误"),
        ("confirm", "j2", "重发我"),
        ("idle",),
    ]
    assert tray_log == ["recording", "polishing", "idle", "error", "error", "idle"]
    assert confirmed == [("j2", "重发我", True)]


def test_cross_platform_singleton_lock_acquires_and_releases(tmp_path: Path):
    lock_file = tmp_path / "daemon.lock"
    lock = try_acquire_singleton_lock(lock_file)
    try:
        assert lock_file.is_file()
        assert lock_file.read_text(encoding="ascii").strip().isdigit()
    finally:
        lock.release()
    # 释放后可重新获取
    lock2 = try_acquire_singleton_lock(lock_file)
    lock2.release()


def test_conflict_scan_with_fake_gsettings():
    def run(cmd, **kw):
        out = ""
        if cmd[:2] == ["gsettings", "list-recursively"] and cmd[2] == "org.gnome.desktop.wm.keybindings":
            out = "org.gnome.desktop.wm.keybindings show-desktop ['<Primary><Alt>d']\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    conflicts = scan_hotkey_conflicts(
        HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+d", repeat="ctrl+alt+r"),
        run=run,
        which=lambda n: "/usr/bin/gsettings",
    )
    assert any("show-desktop" in c for c in conflicts.conflicts), conflicts
    missing = scan_hotkey_conflicts(DEFAULTS, run=run, which=lambda n: None)
    assert missing.conflicts == () and missing.unread and "gsettings 不可用" in missing.unread[0]


def test_startup_issues_wayland_and_x11():
    env_w = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
    issues = startup_issues(
        DEFAULTS,
        environ=env_w,
        which=lambda n: None,
        portal_version=lambda i: None,
        screenshot_tool=None,
        conflicts=lambda b: ConflictScan(conflicts=("c1",), unread=("u1",)),
    )
    assert any("xclip" in i for i in issues)
    assert any("org.freedesktop.portal.Screenshot" in i for i in issues)
    assert all("GDK_BACKEND" not in i for i in issues)
    assert issues[-2] == "c1"
    assert "未能读取" in issues[-1] and "u1" in issues[-1]
    ok = startup_issues(
        DEFAULTS,
        environ={"XDG_SESSION_TYPE": "x11"},
        which=lambda n: f"/usr/bin/{n}",
        portal_version=lambda i: 1,
        screenshot_tool="maim",
        conflicts=lambda b: ConflictScan((), ()),
    )
    assert ok == []


def test_apply_qt_app_icon_prefers_ico_and_falls_back_to_svg(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from _desktop.ui_bridge import apply_qt_app_icon
    from _paths import PROGRAM_ROOT

    class _FakeIcon:
        def __init__(self, path: str, *, null: bool = False) -> None:
            self.path = path
            self._null = null

        def isNull(self) -> bool:
            return self._null

    class _FakeApp:
        def __init__(self) -> None:
            self.icon = None

        def setWindowIcon(self, icon) -> None:
            self.icon = icon

    # 1. 同时存在 .ico 与 .svg 时优先选 .ico
    root_both = tmp_path / "both"
    (root_both / "resources").mkdir(parents=True)
    ico_file = root_both / "resources" / "ollama-voice-input.ico"
    svg_file = root_both / "resources" / "ollama-voice-input.svg"
    ico_file.write_bytes(b"ico")
    svg_file.write_text("<svg/>", encoding="utf-8")
    app1 = _FakeApp()
    assert apply_qt_app_icon(app1, SimpleNamespace(program_root=root_both), icon_factory=_FakeIcon) is True
    assert app1.icon.path == str(ico_file)

    # 2. .ico 加载为空或缺失时回退 .svg
    app2 = _FakeApp()
    assert (
        apply_qt_app_icon(
            app2,
            SimpleNamespace(program_root=root_both),
            icon_factory=lambda p: _FakeIcon(p, null=p.endswith(".ico")),
        )
        is True
    )
    assert app2.icon.path == str(svg_file)

    ico_file.unlink()
    app3 = _FakeApp()
    assert apply_qt_app_icon(app3, SimpleNamespace(program_root=root_both), icon_factory=_FakeIcon) is True
    assert app3.icon.path == str(svg_file)

    # 3. 均缺失返回 False 且不调用 setWindowIcon
    app4 = _FakeApp()
    assert apply_qt_app_icon(app4, SimpleNamespace(program_root=tmp_path / "empty"), icon_factory=_FakeIcon) is False
    assert app4.icon is None

    # 4. 真实 Qt QIcon 加载仓库单源 .ico（headless offscreen）
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    _qt_app = QGuiApplication.instance() or QGuiApplication([])
    app_real = _FakeApp()
    assert apply_qt_app_icon(app_real, SimpleNamespace(program_root=PROGRAM_ROOT)) is True
    assert app_real.icon is not None and not app_real.icon.isNull()
