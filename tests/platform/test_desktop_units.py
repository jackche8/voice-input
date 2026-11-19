"""桌面业务编排与平台辅助单元：CaptureFlow（红十字框选复用原位翻译 + 纯 OCR）、VoiceFlow（热词 + 防409排队）、
DesktopActions 四键、ProductEventLoop 断线退避与 HUD 映射、跨平台单例锁、冲突扫描、启动检查、sd_notify。

全部假对象：不开图形窗口、不截屏、不开麦、不读真实 gsettings、不连 systemd。
"""
from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from _capture import FakeAudioSource
from _contracts import ErrorCode, OcrTextBlock
from _desktop.actions import DesktopActions
from _desktop.api import ApiError
from _desktop.capture_flow import CaptureFlow
from _desktop.delivery import LastDelivered, RepeatAction
from _desktop.event_stream import ProductEventLoop, dispatch_product_event
from _desktop.singleton import acquire_singleton_lock
from _desktop.status import PlatformStatus
from _desktop.voice import VoiceFlow, build_keywords_context_provider
from _focus import StaticFocusProbe
from _hotkey import HotkeyBindings
from _linux.conflicts import ConflictScan, scan_hotkey_conflicts
from _linux.sd_notify import sd_notify
from _linux.startup_check import startup_issues

DEFAULTS = HotkeyBindings(voice="ctrl+alt+z", screenshot="ctrl+alt+x", ocr="ctrl+alt+o", repeat="ctrl+alt+r")


def _status():
    posted: list[dict] = []
    return PlatformStatus(posted.append, start_thread=False), posted


class FakeSelector:
    def __init__(self, rect, mode_label, on_selected, on_cancelled) -> None:
        self.rect = rect
        self.mode_label = mode_label
        self.on_selected = on_selected
        self.on_cancelled = on_cancelled
        self.closed = False

    def close_silent(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class FakeInplaceOverlay:
    def __init__(self, png, rect, *, loading, on_copy_text, on_retry, on_closed) -> None:
        self.png = png
        self.rect = rect
        self.loading = loading
        self.on_copy_text = on_copy_text
        self.on_retry = on_retry
        self.on_closed = on_closed
        self.blocks: list[OcrTextBlock] = []
        self.full_text = ""
        self.full_ocr = ""
        self.error: str | None = None
        self.closed = False

    def set_loading(self) -> None:
        self.loading = True
        self.error = None

    def set_result(self, blocks: list[OcrTextBlock], full_translated_text: str = "", full_ocr_text: str = "") -> None:
        self.loading = False
        self.error = None
        self.blocks = list(blocks)
        self.full_text = full_translated_text
        self.full_ocr = full_ocr_text

    def set_error(self, message: str) -> None:
        self.loading = False
        self.error = message

    def close_silent(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True
        if self.on_closed:
            self.on_closed()


def _make_capture_harness(api, *, capture_fn=None, copy_fn=None):
    status, posted = _status()
    selectors: list[FakeSelector] = []
    overlays: list[FakeInplaceOverlay] = []
    copied: list[str] = []
    toasts: list[tuple[str, str]] = []
    region_calls: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int] | None]] = []

    def open_selector(rect, mode_label, on_selected, on_cancelled):
        sel = FakeSelector(rect, mode_label, on_selected, on_cancelled)
        selectors.append(sel)
        return sel

    def open_overlay(png, rect, *, loading, on_copy_text, on_retry, on_closed):
        ov = FakeInplaceOverlay(
            png,
            rect,
            loading=loading,
            on_copy_text=on_copy_text,
            on_retry=on_retry,
            on_closed=on_closed,
        )
        overlays.append(ov)
        return ov

    def default_copy(text: str) -> bool:
        copied.append(text)
        return True

    def default_capture_region(rect, vrect):
        region_calls.append((rect, vrect))
        return b"region-png"

    flow = CaptureFlow(
        capture_region=capture_fn or default_capture_region,
        open_selector=open_selector,
        open_inplace_overlay=open_overlay,
        copy_clipboard=copy_fn or default_copy,
        notify_toast=lambda msg, kind: toasts.append((msg, kind)),
        api=api,
        status=status,
        invoke_ui=lambda fn: fn(),
        get_virtual_rect=lambda: (0, 0, 1920, 1080),
        capture_delay_s=0.0,
        spawn=lambda fn: fn(),
    )
    flow._test_region_calls = region_calls  # type: ignore[attr-defined]
    return flow, status, posted, selectors, overlays, copied, toasts


# ------------------------------------------------------------------ CaptureFlow


def test_capture_flow_translate_reuses_selector_and_renders_inplace_blocks():
    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def get_config(self) -> dict:
            return {"translate_target_lang": "en"}

        def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
            self.calls.append((target_lang, png.decode("latin1")))
            return {
                "ok": True,
                "text": "Hello World",
                "ocr_text": "你好世界",
                "target_lang": target_lang,
                "image_width": 200,
                "image_height": 80,
                "blocks": [
                    {
                        "bbox": [4, 6, 100, 24],
                        "ocr_text": "你好世界",
                        "translated_text": "Hello World",
                        "bg_color": "#112233",
                        "fg_color": "#ffffff",
                    }
                ],
            }

    api = Api()
    flow, status, posted, selectors, overlays, copied, _toasts = _make_capture_harness(api)

    flow.trigger_translate()
    assert len(selectors) == 1
    assert selectors[0].mode_label == "截屏原位翻译"
    assert selectors[0].rect == (0, 0, 1920, 1080)
    assert flow.overlay_active() is True

    selectors[0].on_selected((100, 200, 300, 120))
    status.drain()
    assert len(overlays) == 1
    ov = overlays[0]
    assert ov.loading is False and ov.error is None
    assert ov.rect == (100, 200, 300, 120)
    assert len(ov.blocks) == 1 and isinstance(ov.blocks[0], OcrTextBlock)
    assert ov.blocks[0].bg_color == "#112233" and ov.full_text == "Hello World" and ov.full_ocr == "你好世界"
    assert flow._test_region_calls == [((100, 200, 300, 120), (0, 0, 1920, 1080))]  # type: ignore[attr-defined]
    assert api.calls == [("en", "region-png")]
    assert [p["event"] for p in posted] == ["busy", "idle"]

    # 工具栏复制按钮走平台剪贴板
    ov.on_copy_text(ov.full_text)
    assert copied == ["Hello World"]

    # 重试按钮重新触发 ocr_inplace
    ov.on_retry()
    assert len(api.calls) == 2

    # 关闭旧浮窗
    flow.close_overlay()
    assert ov.closed is True and flow.overlay_active() is False


def test_capture_flow_translate_error_is_visible_on_overlay_and_status():
    class FailingApi:
        def get_config(self) -> dict:
            return {"translate_target_lang": "zh"}

        def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
            raise ApiError(503, ErrorCode.translate_failed.value, "Ollama 17947 未启动")

    flow, status, posted, selectors, overlays, _copied, _toasts = _make_capture_harness(FailingApi())
    flow.trigger_translate()
    selectors[0].on_selected((10, 20, 200, 90))
    status.drain()

    assert overlays[0].loading is False
    assert overlays[0].error == "Ollama 17947 未启动"
    assert posted[-1]["event"] == "error"
    assert posted[-1]["error"] == ErrorCode.translate_failed.value
    assert "Ollama 17947" in posted[-1]["message"]


def test_capture_flow_pure_ocr_reuses_same_selector_and_copies_with_toast():
    class OcrApi:
        def __init__(self) -> None:
            self.target_langs: list[str] = []

        def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
            self.target_langs.append(target_lang)
            return {
                "ok": True,
                "text": "跨平台纯OCR",
                "ocr_text": "跨平台纯OCR",
                "target_lang": "raw",
                "image_width": 100,
                "image_height": 40,
                "blocks": [],
            }

    api = OcrApi()
    flow, status, posted, selectors, overlays, copied, toasts = _make_capture_harness(api)

    flow.trigger_ocr()
    assert len(selectors) == 1
    assert selectors[0].mode_label == "纯 OCR 识别 · 自动复制"

    selectors[0].on_selected((10, 10, 100, 40))
    status.drain()
    assert overlays == []  # 纯 OCR 不弹翻译浮窗
    assert flow._test_region_calls == [((10, 10, 100, 40), (0, 0, 1920, 1080))]  # type: ignore[attr-defined]
    assert api.target_langs == ["raw"]
    assert copied == ["跨平台纯OCR"]
    assert toasts == [("已复制 OCR 文本（7 字）", "ok")]
    assert [p["event"] for p in posted] == ["busy", "idle"]


def test_capture_flow_pure_ocr_toast_survives_async_sse_busy_and_idle(monkeypatch):
    """P0-2 回归：真实 PlatformStatus (20ms HTTP→SSE 延迟) + QtUiBridge + 真实 VoiceHudOverlay。

    断言纯 OCR 完成后 250ms 内，字数 Toast「已复制 OCR 文本（7 字）」始终保持可见，
    不被迟到的 busy 或 idle SSE 事件冲掉，且托盘最终回到 idle。
    """
    import os
    from PySide6.QtWidgets import QApplication

    from _desktop.ui_bridge import QtUiBridge
    from _ui_desktop import VoiceHudOverlay

    app = QApplication.instance()
    if app is None:
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])

    for delivery_mode in ("slow_http", "async_sse"):
        hud = VoiceHudOverlay(available_screen_rect=(0, 0, 1280, 800))
        bridge = QtUiBridge()
        tray_states: list[str] = []
        posted_events: list[str] = []
        dispatched_events: list[str] = []
        pending_timers: list[threading.Timer] = []

        class FakeTray:
            def set_state(self, state: str) -> None:
                tray_states.append(state)

        tray = FakeTray()

        def _dispatch(ev: dict) -> None:
            dispatched_events.append(str(ev.get("event") or ""))
            dispatch_product_event(ev, hud=hud, tray=tray)

        def post_platform_event(ev: dict) -> None:
            posted_events.append(str(ev.get("event") or ""))
            if delivery_mode == "slow_http":
                time.sleep(0.02)
                bridge.post(lambda _ev=dict(ev): _dispatch(_ev))
            else:
                # 真实 SSE 为单条有序流，按投递序号错开定时器防止两条同毫秒 Timer 竞态倒序
                delay = 0.02 * len(posted_events)
                timer = threading.Timer(delay, lambda _ev=dict(ev): bridge.post(lambda: _dispatch(_ev)))
                timer.daemon = True
                pending_timers.append(timer)
                timer.start()

        status = PlatformStatus(post_platform_event, start_thread=True)

        class FastOcrApi:
            def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
                del png, lang
                assert target_lang == "raw"
                return {
                    "ok": True,
                    "text": "跨平台纯OCR",
                    "ocr_text": "跨平台纯OCR",
                    "target_lang": "raw",
                    "blocks": [],
                }

        selectors: list[FakeSelector] = []
        selector_ready = threading.Event()
        toast_shown = threading.Event()
        copied: list[str] = []

        def open_selector(rect, mode_label, on_selected, on_cancelled):
            sel = FakeSelector(rect, mode_label, on_selected, on_cancelled)
            selectors.append(sel)
            selector_ready.set()
            return sel

        def notify_toast(msg: str, kind: str) -> None:
            hud.show_toast(msg, kind=kind)
            toast_shown.set()

        flow = CaptureFlow(
            capture_region=lambda rect, vrect: b"crop-ocr",
            open_selector=open_selector,
            open_inplace_overlay=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("纯 OCR 不应弹浮窗")),
            copy_clipboard=lambda text: (copied.append(text) or True),
            notify_toast=notify_toast,
            api=FastOcrApi(),
            status=status,
            invoke_ui=bridge.post,
            get_virtual_rect=lambda: (0, 0, 1280, 800),
            capture_delay_s=0.0,
        )

        try:
            flow.trigger_ocr()
            deadline = time.monotonic() + 2.0
            while not selector_ready.is_set() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            assert selector_ready.is_set() and len(selectors) == 1

            # 立刻完成框选，故意令 _start_capture 的 busy 与 _ocr_worker 的 idle 在 20ms 后才到 SSE
            selectors[0].on_selected((10, 10, 100, 40))

            deadline = time.monotonic() + 2.0
            while not toast_shown.is_set() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            assert toast_shown.is_set()
            assert copied == ["跨平台纯OCR"]

            # 250ms 观察窗口内持续处理 Qt 事件：滞后到达的 busy / idle 均绝不可冲掉 Toast
            watch_end = time.monotonic() + 0.25
            while time.monotonic() < watch_end:
                app.processEvents()
                assert hud.isVisible() is True, (delivery_mode, hud.mode, hud.capsule_text, dispatched_events)
                assert hud.mode == "toast", (delivery_mode, hud.mode, hud.capsule_text, dispatched_events)
                assert hud.capsule_text == "已复制 OCR 文本（7 字）", (delivery_mode, hud.capsule_text)
                time.sleep(0.01)

            app.processEvents()
            assert posted_events == ["busy", "idle"], (delivery_mode, posted_events)
            assert dispatched_events == ["busy", "idle"], (delivery_mode, dispatched_events)
            assert tray_states[-1] == "idle", (delivery_mode, tray_states)

            # 非空闲/非忙碌的实时高优事件（如新一轮录音）仍应立刻切出 Toast
            dispatch_product_event({"event": "recording"}, hud=hud, tray=tray)
            assert hud.mode == "recording"
            assert tray_states[-1] == "recording"
        finally:
            for timer in pending_timers:
                timer.cancel()
            status.close()
            hud.close()
            hud.deleteLater()
            app.processEvents()


def test_capture_flow_ocr_failure_and_cancel_are_visible():
    class EmptyOcrApi:
        def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
            return {"ok": True, "text": "", "ocr_text": "   ", "target_lang": "raw", "blocks": []}

    flow, status, posted, selectors, _overlays, copied, toasts = _make_capture_harness(EmptyOcrApi())
    flow.trigger_ocr()
    selectors[0].on_cancelled()
    status.drain()
    assert posted[-1]["event"] == "idle" and flow.overlay_active() is False

    flow.trigger_ocr()
    selectors[-1].on_selected((0, 0, 50, 50))
    status.drain()
    assert copied == [] and toasts == []
    assert posted[-1]["event"] == "error" and posted[-1]["error"] == ErrorCode.ocr_failed.value


def test_capture_flow_direct_region_capture_and_repeat_storm_guard():
    """根治回归：先透明框选不预抓全屏、选区期间重复触发热键不产生风暴、松手后仅截取目标区域。"""
    class Api:
        def __init__(self) -> None:
            self.seen_pngs: list[bytes] = []

        def ocr_inplace(self, png: bytes, *, target_lang: str, lang=None) -> dict:
            self.seen_pngs.append(png)
            return {"ok": True, "text": "直接选区OCR", "ocr_text": "直接选区OCR", "target_lang": target_lang, "blocks": []}

    api = Api()
    flow, status, posted, selectors, _overlays, copied, toasts = _make_capture_harness(api)

    # 连续触发 20 次（模拟长按快捷键连发）→ 仅开 1 个框选器、0 次抓屏、0 次 busy
    for _ in range(20):
        flow.trigger_ocr()
    status.drain()
    assert len(selectors) == 1
    assert flow._test_region_calls == []  # type: ignore[attr-defined]
    assert posted == []

    # 透明框选器松手仅回传逻辑坐标 → 单路径触发 1 次 capture_region
    selectors[0].on_selected((120, 240, 360, 90))
    status.drain()
    assert flow._test_region_calls == [((120, 240, 360, 90), (0, 0, 1920, 1080))]  # type: ignore[attr-defined]
    assert api.seen_pngs == [b"region-png"]
    assert copied == ["直接选区OCR"]
    assert toasts == [("已复制 OCR 文本（7 字）", "ok")]


def test_capture_screen_failure_is_visible():
    def boom(_rect, _vrect):
        raise RuntimeError("Portal 拒绝授权")

    flow, status, posted, selectors, overlays, _copied, _toasts = _make_capture_harness(
        api=object(), capture_fn=boom
    )
    flow.trigger_translate()
    assert len(selectors) == 1
    selectors[0].on_selected((10, 20, 200, 100))
    status.drain()
    assert overlays == []
    assert posted[-1]["event"] == "error" and "Portal 拒绝授权" in posted[-1]["message"]


def test_capture_flow_reentrant_close_no_deadlock_and_generation_isolation():
    """P1-4 回归：过期 selector/overlay 的 close() 重入回调不死锁，且旧代次不得清零新代次 _selector_opening。"""
    status, posted = _status()
    ui_queue: list = []
    selector_handles: list = []
    overlay_handles: list = []

    class ReentrantSelector:
        """故意只提供 close()（无 close_silent），在 close() 内同步回调 on_cancelled 重入 CaptureFlow._lock。"""

        def __init__(self, on_selected, on_cancelled) -> None:
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

        def set_loading(self) -> None:
            pass

        def set_result(self, blocks, full_translated_text="", full_ocr_text="") -> None:
            pass

        def set_error(self, message: str) -> None:
            pass

        def close(self) -> None:
            self.closed = True
            self.on_closed()

    selector_stage = {"n": 0}

    def open_selector(_rect, _label, on_selected, on_cancelled):
        selector_stage["n"] += 1
        sel = ReentrantSelector(on_selected, on_cancelled)
        selector_handles.append(sel)
        if selector_stage["n"] == 1:
            # 模拟代次 1 打开框选器途中被取消并立刻开启代次 2（代次 2 仍在 ui_queue 中待打开）
            flow.close_overlay()
            flow.trigger_translate()
        return sel

    def open_overlay(_png, _rect, *, loading, on_copy_text, on_retry, on_closed):
        del loading, on_copy_text, on_retry
        ov = ReentrantOverlay(on_closed)
        overlay_handles.append(ov)
        # 模拟浮窗打开途中被 Esc / close_overlay 取消，令当前代次过期
        flow.close_overlay()
        return ov

    flow = CaptureFlow(
        capture_region=lambda _r, _v: b"png",
        open_selector=open_selector,
        open_inplace_overlay=open_overlay,
        copy_clipboard=lambda _t: True,
        notify_toast=lambda _m, _k: None,
        api=object(),
        status=status,
        invoke_ui=ui_queue.append,
        get_virtual_rect=lambda: (0, 0, 1920, 1080),
        capture_delay_s=0.0,
        spawn=lambda fn: fn(),
    )

    # 1. 触发代次 1：尚未落到 UI 线程时 overlay_active() 就应为 True（含 _selector_opening）
    flow.trigger_translate()
    assert flow.overlay_active() is True
    assert len(ui_queue) == 1

    # 执行代次 1 的 _open_selector_ui：内部取消代次 1 并启动代次 2 → 代次 1 过期关闭不死锁，且不误清代次 2 的 _selector_opening
    ui_queue.pop(0)()
    assert selector_handles[0].closed is True
    assert flow._selector_opening is True
    assert flow.overlay_active() is True
    assert len(ui_queue) == 1  # 代次 2 待执行的 UI 回调
    # 代次 2 仍在 _selector_opening 中，重复触发必须被拦住
    flow.trigger_ocr()
    assert len(ui_queue) == 1

    # 2. 执行代次 2 的 _open_selector_ui 并完成框选 → 进入 open_overlay 时被取消 → 过期 overlay.close() 重入 on_closed 不死锁
    ui_queue.pop(0)()
    assert flow._selector_opening is False
    assert len(selector_handles) == 2 and selector_handles[1].closed is False

    selector_handles[1].on_selected((10, 20, 100, 80))
    assert len(ui_queue) == 1
    ui_queue.pop(0)()
    assert len(overlay_handles) == 1 and overlay_handles[0].closed is True
    assert flow.overlay_active() is False
    status.drain()
    assert posted[-1]["event"] == "idle"


def test_capture_flow_close_overlay_during_capturing_resets_idle():
    """P1-4 回归：在 _capturing 延迟等待或抓屏阶段调用 close_overlay，状态必须回到 idle 不卡 busy。"""
    status, posted = _status()
    selectors: list[FakeSelector] = []
    overlays: list[FakeInplaceOverlay] = []
    region_calls: list = []

    def cancel_during_sleep(_delay: float) -> None:
        assert flow._capturing is True
        flow.close_overlay()

    flow = CaptureFlow(
        capture_region=lambda r, v: (region_calls.append((r, v)) or b"png"),
        open_selector=lambda rect, label, on_sel, on_cancel: (
            selectors.append(FakeSelector(rect, label, on_sel, on_cancel)) or selectors[-1]
        ),
        open_inplace_overlay=lambda *a, **kw: (
            overlays.append(FakeInplaceOverlay(*a, **kw)) or overlays[-1]
        ),
        copy_clipboard=lambda _t: True,
        notify_toast=lambda _m, _k: None,
        api=object(),
        status=status,
        invoke_ui=lambda fn: fn(),
        get_virtual_rect=lambda: (0, 0, 1920, 1080),
        capture_delay_s=0.05,
        sleep=cancel_during_sleep,
        spawn=lambda fn: fn(),
    )

    flow.trigger_translate()
    selectors[0].on_selected((10, 20, 120, 80))
    status.drain()
    assert region_calls == []
    assert overlays == []
    assert [p["event"] for p in posted] == ["busy", "idle"]
    assert flow._capturing is False

    # 也在 capture_region 执行中取消，同样回到 idle 且不弹出浮窗
    posted.clear()
    flow._sleep = lambda _d: None
    flow.capture_region = lambda r, v: (flow.close_overlay(), b"late-png")[1]
    flow.trigger_translate()
    selectors[-1].on_selected((10, 20, 120, 80))
    status.drain()
    assert overlays == []
    assert [p["event"] for p in posted] == ["busy", "idle"]


def test_ui_bridge_configure_qt_platform_env_and_open_live_selector(monkeypatch):
    """P2-4：覆盖 configure_qt_platform_env 与 open_live_selector 的单屏/多屏与参数契约。"""
    import os
    import sys

    from _desktop.ui_bridge import configure_qt_platform_env, open_live_selector

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

    # 5. open_live_selector：不传 screenshot_png；单屏 showFullScreen，多屏 show
    calls: list[str] = []
    seen_kwargs: list[dict] = []

    class DummySelector:
        def __init__(self, **kw) -> None:
            assert "screenshot_png" not in kw
            seen_kwargs.append(kw)

        def showFullScreen(self) -> None:
            calls.append("showFullScreen")

        def show(self) -> None:
            calls.append("show")

        def raise_(self) -> None:
            calls.append("raise_")

        def activateWindow(self) -> None:
            calls.append("activateWindow")

    single_app = type("SingleApp", (), {"screens": lambda self: [object()]})()
    on_sel = lambda _rect: None
    on_cancel = lambda: None
    sel1 = open_live_selector(single_app, DummySelector, (0, 0, 1920, 1080), "截屏原位翻译", on_sel, on_cancel)
    assert isinstance(sel1, DummySelector)
    assert seen_kwargs[-1] == {
        "virtual_geometry": (0, 0, 1920, 1080),
        "mode_label": "截屏原位翻译",
        "on_selected": on_sel,
        "on_cancelled": on_cancel,
    }
    assert calls == ["showFullScreen", "raise_", "activateWindow"]

    calls.clear()
    multi_app = type("MultiApp", (), {"screens": lambda self: [object(), object()]})()
    open_live_selector(multi_app, DummySelector, (-1920, 0, 3840, 1080), "纯 OCR 识别", on_sel, on_cancel)
    assert calls == ["show", "raise_", "activateWindow"]


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
    from _linux.hotkey_actions import CallbackHotkeyActions, HotkeyActionMixin, HotkeyContext
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
        return mask, ord(parts[-1])

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

    # 2. LinuxHotkeyRig + HotkeyManager + HotkeyActionMixin
    linux_starts: list[str] = []
    released_hotkeys: list[str] = []

    class FakeJanitor:
        evdev_any_modifier_down = None

        def release_hotkey(self, hk: str) -> None:
            released_hotkeys.append(hk)

    class DummyGrabber(HotkeyActionMixin):
        def __init__(self, ctx: HotkeyContext) -> None:
            self._init_hotkey_actions(ctx)

        def cleanup(self) -> None:
            pass

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
    mgr = HotkeyManager(ctx)
    grabber = DummyGrabber(ctx)
    mgr.active = grabber
    linux_rig = LinuxHotkeyRig(mgr)
    assert isinstance(linux_rig, HotkeyRig)

    grabber._on_voice_press()
    assert mgr.voice_active is True and linux_starts == ["start"]

    linux_rig.reset_voice()
    assert mgr.voice_active is False
    assert grabber.voice_active is False
    assert released_hotkeys == [DEFAULTS.voice]
    assert linux_starts == ["start"]  # reset_voice 不重复调用 voice_stop

    # 再次按下可立即触发下一句录音
    grabber._on_voice_press()
    assert mgr.voice_active is True and linux_starts == ["start", "start"]


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

        def overlay_active(self):
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
    actions.repeat()
    assert actions.overlay_active() is True
    actions.close_overlay()
    assert log == ["vstart", "vstop", "translate", "ocr", "repeat", "close"]


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
        def set_busy(self, msg=""): hud_log.append(("busy", msg))
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
        {"event": "busy"},
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
        ("busy", "处理中…"),
        ("error", "测试错误"),
        ("confirm", "j2", "重发我"),
        ("idle",),
    ]
    assert tray_log == ["recording", "polishing", "idle", "busy", "error", "error", "idle"]
    assert confirmed == [("j2", "重发我", True)]


def test_cross_platform_singleton_lock_acquires_and_releases(tmp_path: Path):
    lock_file = tmp_path / "daemon.lock"
    lock = acquire_singleton_lock(lock_file)
    try:
        assert lock_file.is_file()
        assert lock_file.read_text(encoding="ascii").strip().isdigit()
    finally:
        lock.release()
    # 释放后可重新获取
    lock2 = acquire_singleton_lock(lock_file)
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


def test_sd_notify_noop_without_socket_and_sends_with_socket():
    assert sd_notify("READY=1", environ={}) is False
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(path)
        try:
            assert sd_notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": path}) is True
            assert srv.recv(64) == b"WATCHDOG=1"
        finally:
            srv.close()
