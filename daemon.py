"""应用主进程入口（薄装配层 · PySide6-Essentials，零 GTK / WebKit 依赖）。

平台能力来自 `_platform`，原生视图来自 `_ui_desktop`，业务流程来自 `_desktop`：本地服务（Ollama、识别服务、服务端）
由看护层拉起与照看，主界面是独立的窗口进程，同一用户只运行一个实例（第二次启动只唤起主窗口）。本文件不含业务规则。
`--background`（开机自启）只启动不弹主窗口。
"""
from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from _config import ConfigStore
from _contracts import ErrorCode
from _desktop.actions import DesktopActions
from _desktop.api import DesktopApi
from _desktop.capture_flow import CaptureFlow
from _desktop.delivery import DeliveryLoop, LastDelivered, PlatformFocusProbe, PlatformInjector, RepeatAction
from _desktop.desktop_integration import apply_autostart, ensure_portal_app_entry
from _desktop.event_stream import ProductEventLoop, dispatch_product_event
from _desktop.main_window import MainWindow
from _desktop.single_instance import SingleInstanceServer, run_as_second_instance, socket_name
from _desktop.singleton import try_acquire_singleton_lock
from _desktop.status import PlatformStatus
from _desktop.supervisor import create_supervisor
from _desktop.ui_bridge import (
    QtUiBridge, configure_qt_platform_env, open_annotator, open_live_selector, open_translate_overlay,
    query_virtual_screen_rect,
)
from _desktop.voice import VoiceFlow, build_keywords_context_provider, new_job_id
from _hotkey import HotkeyBindings, format_hotkey
from _paths import APP_DISPLAY_NAME, default_app_paths, pictures_dir
from _platform import create_platform_adapter
from _platform._common import SoundDeviceSource
from _platform.certs import ensure_tls_certificates
from _platform.child_process import attach_app_log
from _ports import local_ports
from _ui_desktop import AnnotateOverlay, InplaceTranslateOverlay, RedCrosshairSelector, StudioTrayIcon, VoiceHudOverlay
from _version import APP_VERSION

VOICE_CHECK_MS = 2000
VOICE_MAX_SECONDS = 120.0
SUPERVISOR_STOP_S = 60.0


def load_bindings(config: Path) -> HotkeyBindings:
    cfg = ConfigStore(config).load()  # 与服务端同一配置文件（AppPaths.config），只读
    return HotkeyBindings(
        voice=cfg.hotkey_voice, screenshot=cfg.hotkey_screenshot, ocr=cfg.hotkey_ocr,
        repeat=cfg.hotkey_repeat, annotate=cfg.hotkey_annotate,
    )


def main() -> None:
    paths = default_app_paths(os.environ)
    origin = local_ports(os.environ).server_origin
    configure_qt_platform_env()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Ollama Voice Input")
    app.setQuitOnLastWindowClosed(False)
    lock = try_acquire_singleton_lock(paths.lock)
    if lock is None:  # 已有实例在运行：请它打开主窗口，本进程退出
        sys.exit(run_as_second_instance(paths))
    app_log = attach_app_log(paths.logs / "app.log")
    atexit.register(app_log.close)
    ensure_tls_certificates(paths.cert_dir)  # 首次运行先生成本地 CA 与服务端证书（本机 API 客户端要用它校验）
    config = ConfigStore(paths.config)
    bindings = load_bindings(paths.config)
    platform = create_platform_adapter()
    atexit.register(platform.close)
    bridge = QtUiBridge()

    api = DesktopApi(origin, cert=paths.cert)
    status = PlatformStatus(api.post_platform_event, post_components=api.post_components)
    probe = PlatformFocusProbe(platform)
    injector = PlatformInjector(platform)
    last = LastDelivered()
    hud = VoiceHudOverlay()
    capture_flow = CaptureFlow(
        capture_region=lambda rect, vrect: platform.capture_region(rect, virtual_rect=vrect),
        open_selector=lambda rect, label, on_sel, on_cancel: open_live_selector(
            app, RedCrosshairSelector, rect, label, on_sel, on_cancel
        ),
        open_inplace_overlay=lambda png, rect, **kw: open_translate_overlay(app, InplaceTranslateOverlay, png, rect, **kw),
        open_annotator=lambda png, rect, **kw: open_annotator(app, AnnotateOverlay, png, rect, **kw),
        copy_clipboard=lambda text: platform.write_clipboard_text(text) is None,
        copy_image=platform.write_clipboard_png,
        save_dir=lambda: pictures_dir(os.environ, os_name=platform.os_name) / APP_DISPLAY_NAME,
        api=api, status=status, invoke_ui=bridge.post, get_virtual_rect=lambda: query_virtual_screen_rect(app),
    )
    voice = VoiceFlow(
        api=api, status=status, probe=probe, source_factory=SoundDeviceSource, beep=platform.play_beep,
        context_provider=build_keywords_context_provider(api),
    )
    repeat_action = RepeatAction(api=api, last=last, probe=probe, status=status, job_ids=new_job_id)
    actions = DesktopActions(voice=voice, capture=capture_flow, repeat=repeat_action)
    main_window = MainWindow(paths=paths, origin=origin, environ=os.environ,
                             on_failed=lambda message: status.error(ErrorCode.dependency_failed, message))
    tray = StudioTrayIcon(
        on_open_home=lambda: main_window.show("/"), on_screenshot_translate=actions.screenshot,
        on_pure_ocr=actions.ocr, on_screenshot_annotate=actions.annotate, on_open_setup=lambda: main_window.show("/setup"),
        on_toggle_hud=hud.set_enabled, on_quit=app.quit, on_retry_server=lambda: supervisor.retry("server"),
        hud_enabled=hud.enabled,
    )
    tray.show()
    supervisor = create_supervisor(paths, os.environ, status=status, tray=tray, ui=bridge.post,
                                   on_server_running=None if "--background" in sys.argv[1:] else lambda: main_window.show("/"))
    single = SingleInstanceServer(socket_name(paths), on_show=main_window.show, on_quit=app.quit)  # quit 与托盘「退出」同一路径
    single.listen()

    def sync_autostart() -> None:  # 启动时与设置页改了配置（config_changed）时都按配置落地
        try:
            apply_autostart(platform, paths, config.load().autostart_enabled)
        except OSError as exc:
            status.error(ErrorCode.config_io, f"开机自启设置失败：{exc}")

    sync_autostart()
    if platform.os_name == "linux":
        ensure_portal_app_entry(paths, os.environ)  # 全局快捷键门户按它认本应用
    supervisor.start()

    def _on_confirm_resend(job_id: str, text: str, confirmed: bool) -> None:
        threading.Thread(
            target=lambda: repeat_action.confirm_resend(job_id, text, confirmed), name="confirm-resend", daemon=True
        ).start()

    def _on_component_command(action: str, component: str) -> None:
        if action == "retry":
            supervisor.retry(component)

    event_loop = ProductEventLoop(
        iter_events=api.iter_product_events,
        on_event=lambda ev: bridge.post(
            lambda _ev=ev: dispatch_product_event(_ev, hud=hud, tray=tray, on_confirm_resend=_on_confirm_resend,
                                                  on_component_command=_on_component_command,
                                                  on_config_changed=sync_autostart)
        ),
        on_connected=status.resend_on_connect,
    )
    event_loop.start()

    hotkey_rig = platform.create_hotkey_rig(
        bindings, on_voice_start=actions.voice_start, on_voice_stop=actions.voice_stop, on_screenshot=actions.screenshot,
        on_ocr=actions.ocr, on_repeat=actions.repeat, on_annotate=actions.annotate, overlay_active=actions.overlay_active,
        close_overlay=actions.close_overlay, on_error=status.set_hotkey_error,
    )
    hotkey_rig.start()

    delivery = DeliveryLoop(jobs=api.jobs, injector=injector, last=last)
    threading.Thread(target=delivery.run_forever, name="jobs-delivery", daemon=True).start()

    print(f"Ollama Voice Input v{APP_VERSION} 已启动 ({platform.os_name}/{platform.session_type})，热键后端 "
          f"{hotkey_rig.backend_name}，快捷键 语音 {format_hotkey(bindings.voice)} · 原位翻译 {format_hotkey(bindings.screenshot)}"
          f" · 纯 OCR {format_hotkey(bindings.ocr)} · 重复 {format_hotkey(bindings.repeat)}"
          f" · 截屏标注 {format_hotkey(bindings.annotate)}", flush=True)

    for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, lambda _s, _f: bridge.post(app.quit))

    signal_pump = QTimer()
    signal_pump.setInterval(500)
    signal_pump.timeout.connect(lambda: None)
    signal_pump.start()

    def _on_voice_check() -> None:
        if voice.stop_if_exceeded(VOICE_MAX_SECONDS):
            hotkey_rig.reset_voice()

    voice_timer = QTimer()
    voice_timer.setInterval(VOICE_CHECK_MS)
    voice_timer.timeout.connect(_on_voice_check)
    voice_timer.start()

    try:
        app.exec()
    except KeyboardInterrupt:
        pass
    finally:
        voice_timer.stop()
        signal_pump.stop()
        event_loop.request_stop()
        delivery.stop()
        main_window.close()
        try:
            supervisor.stop(SUPERVISOR_STOP_S)  # 逆序结束服务端、识别服务、Ollama 的会话
        except RuntimeError as exc:  # 时限内没结束：记下原因照常退出，残留由 PDEATHSIG 与下次启动的记录清理收尾
            print(f"  看护: {exc}", flush=True)
        api.close()
        event_loop.stop(timeout=0.5)
        hotkey_rig.stop()
        capture_flow.close_overlay()
        single.close()
        hud.close()
        tray.close()
        status.close()
        platform.close()
        app_log.close()
        lock.release()


if __name__ == "__main__":
    main()
