"""跨平台桌面守护进程入口（薄装配层 · PySide6-Essentials，零 GTK / WebKit 依赖）。

所有平台能力来自 `_platform`（Linux / Windows / macOS），原生视图来自 `_ui_desktop`
（红十字框选器、原位背景遮挡翻译浮窗、状态胶囊与双行大字幕、系统托盘），业务流程来自
`_desktop`（录音排队上传、投递循环、截屏翻译与纯 OCR 复用、SSE 断线退避重连）。本文件不含业务规则。
"""
from __future__ import annotations

from _activation import exit_if_blocked

import atexit
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from _config import ConfigStore
from _desktop.actions import DesktopActions
from _desktop.api import DesktopApi
from _desktop.capture_flow import CaptureFlow
from _desktop.delivery import (
    DeliveryLoop,
    LastDelivered,
    PlatformFocusProbe,
    PlatformInjector,
    RepeatAction,
)
from _desktop.event_stream import ProductEventLoop, dispatch_product_event
from _desktop.singleton import acquire_singleton_lock
from _desktop.status import PlatformStatus
from _desktop.ui_bridge import (
    QtUiBridge,
    configure_qt_platform_env,
    open_live_selector,
    open_translate_overlay,
    open_url_async,
    query_virtual_screen_rect,
)
from _desktop.voice import VoiceFlow, build_keywords_context_provider, new_job_id
from _hotkey import HotkeyBindings, format_hotkey
from _platform import create_platform_adapter
from _platform._common import SoundDeviceSource
from _ui_desktop import (
    InplaceTranslateOverlay,
    RedCrosshairSelector,
    StudioTrayIcon,
    VoiceHudOverlay,
)

VERSION = "3.2.0"
PROJECT_ROOT = Path(__file__).resolve().parent
API_ORIGIN = "https://127.0.0.1:17945"
CERT = PROJECT_ROOT / "certs" / "cert.pem"
CONFIG = PROJECT_ROOT / ".config.json"  # 与 _v3.production.ProductionPaths.config 同一文件，只读
WATCHDOG_MS = 2000
VOICE_MAX_SECONDS = 120.0


def load_bindings() -> HotkeyBindings:
    cfg = ConfigStore(CONFIG).load()
    return HotkeyBindings(
        voice=cfg.hotkey_voice,
        screenshot=cfg.hotkey_screenshot,
        ocr=cfg.hotkey_ocr,
        repeat=cfg.hotkey_repeat,
    )


def main() -> None:
    exit_if_blocked()
    lock = acquire_singleton_lock()
    bindings = load_bindings()
    platform = create_platform_adapter()
    atexit.register(platform.close)

    notify_systemd: Callable[[str], object] = lambda _state: False
    if platform.os_name == "linux":
        from _linux.sd_notify import sd_notify

        notify_systemd = sd_notify

    configure_qt_platform_env()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Ollama Voice Input")
    app.setQuitOnLastWindowClosed(False)
    bridge = QtUiBridge()

    api = DesktopApi(API_ORIGIN, cert=CERT)
    status = PlatformStatus(api.post_platform_event)
    probe = PlatformFocusProbe(platform)
    injector = PlatformInjector(platform)
    last = LastDelivered()
    hud = VoiceHudOverlay()

    capture_flow = CaptureFlow(
        capture_region=lambda rect, vrect: platform.capture_region(rect, virtual_rect=vrect),
        open_selector=lambda rect, mode_label, on_sel, on_cancel: open_live_selector(
            app, RedCrosshairSelector, rect, mode_label, on_sel, on_cancel
        ),
        open_inplace_overlay=lambda png, rect, **kw: open_translate_overlay(InplaceTranslateOverlay, png, rect, **kw),
        copy_clipboard=lambda text: platform.write_clipboard_text(text) is None,
        notify_toast=lambda msg, kind: hud.show_toast(msg, kind=kind),
        api=api,
        status=status,
        invoke_ui=bridge.post,
        get_virtual_rect=lambda: query_virtual_screen_rect(app),
    )
    voice = VoiceFlow(
        api=api,
        status=status,
        probe=probe,
        source_factory=SoundDeviceSource,
        beep=platform.play_beep,
        context_provider=build_keywords_context_provider(api),
    )
    repeat_action = RepeatAction(api=api, last=last, probe=probe, status=status, job_ids=new_job_id)
    actions = DesktopActions(voice=voice, capture=capture_flow, repeat=repeat_action)
    tray = StudioTrayIcon(
        on_open_home=lambda: open_url_async(f"{API_ORIGIN}/"),
        on_screenshot_translate=actions.screenshot,
        on_pure_ocr=actions.ocr,
        on_open_setup=lambda: open_url_async(f"{API_ORIGIN}/setup"),
        on_toggle_hud=hud.set_enabled,
        on_quit=app.quit,
        hud_enabled=hud.enabled,
    )
    tray.show()

    def _on_confirm_resend(job_id: str, text: str, confirmed: bool) -> None:
        threading.Thread(
            target=lambda: repeat_action.confirm_resend(job_id, text, confirmed), name="confirm-resend", daemon=True
        ).start()

    event_loop = ProductEventLoop(
        iter_events=api.iter_product_events,
        on_event=lambda ev: bridge.post(
            lambda _ev=ev: dispatch_product_event(_ev, hud=hud, tray=tray, on_confirm_resend=_on_confirm_resend)
        ),
        on_connected=status.resend_hotkey_error,
    )
    event_loop.start()

    hotkey_rig = platform.create_hotkey_rig(
        bindings,
        on_voice_start=actions.voice_start,
        on_voice_stop=actions.voice_stop,
        on_screenshot=actions.screenshot,
        on_ocr=actions.ocr,
        on_repeat=actions.repeat,
        overlay_active=actions.overlay_active,
        close_overlay=actions.close_overlay,
        on_error=status.set_hotkey_error,
    )
    hotkey_rig.start()

    delivery = DeliveryLoop(jobs=api.jobs, injector=injector, last=last)
    threading.Thread(target=delivery.run_forever, name="jobs-delivery", daemon=True).start()

    print(f"Ollama Voice Input v{VERSION} 跨平台守护进程已启动 ({platform.os_name}/{platform.session_type})", flush=True)
    print(f"  热键后端:  {hotkey_rig.backend_name} | 工作台: {API_ORIGIN}/", flush=True)
    print(
        f"  快捷键:    语音 {format_hotkey(bindings.voice)} · 原位翻译 {format_hotkey(bindings.screenshot)}"
        f" · 纯 OCR {format_hotkey(bindings.ocr)} · 重复 {format_hotkey(bindings.repeat)}",
        flush=True,
    )

    for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, lambda _s, _f: bridge.post(app.quit))

    signal_pump = QTimer()
    signal_pump.setInterval(500)
    signal_pump.timeout.connect(lambda: None)
    signal_pump.start()

    def _on_watchdog_tick() -> None:
        notify_systemd("WATCHDOG=1")
        if voice.stop_if_exceeded(VOICE_MAX_SECONDS):
            hotkey_rig.reset_voice()

    watchdog_timer = QTimer()
    watchdog_timer.setInterval(WATCHDOG_MS)
    watchdog_timer.timeout.connect(_on_watchdog_tick)
    watchdog_timer.start()
    notify_systemd("READY=1")

    try:
        app.exec()
    except KeyboardInterrupt:
        pass
    finally:
        watchdog_timer.stop()
        signal_pump.stop()
        event_loop.request_stop()
        delivery.stop()
        api.close()
        event_loop.stop(timeout=0.5)
        hotkey_rig.stop()
        capture_flow.close_overlay(reset_status=False)
        hud.close()
        tray.close()
        status.close()
        platform.close()
        lock.release()


if __name__ == "__main__":
    main()
