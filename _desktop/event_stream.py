"""产品事件流（GET /api/v1/events）后台断线重连循环与 HUD/托盘映射。

单一职责：
- 消费 `DesktopApi.iter_product_events()`，连接断开或异常时按指数退避（1s → 2s → 4s → 8s → 15s）
  自动重连，绝不永久死亡；
- 将结构化产品事件映射为 `VoiceHudOverlay` 与 `StudioTrayIcon` 状态更新（在 UI 主线程执行）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 15.0


def dispatch_product_event(
    event: dict[str, Any],
    *,
    hud: Any,
    tray: Any = None,
    on_confirm_resend: Callable[[str, str, bool], None] | None = None,
) -> None:
    """将单条产品 SSE 事件字典映射到 `VoiceHudOverlay` 与 `StudioTrayIcon`（应在 UI 线程调用）。"""
    kind = str(event.get("event") or "").strip().lower()
    if kind == "idle":
        if getattr(hud, "mode", None) != "toast":
            hud.set_idle()
        if tray is not None:
            tray.set_state("idle")
        return

    if kind == "recording":
        hud.set_recording()
        if tray is not None:
            tray.set_state("recording")
        return

    if kind == "partial":
        hud.set_partial(
            stable_text=str(event.get("stable_text") or ""),
            tail_text=str(event.get("tail_text") or ""),
        )
        return

    if kind == "polishing":
        hud.set_polishing()
        if tray is not None:
            tray.set_state("polishing")
        return

    if kind == "typed":
        hud.set_typed(text=str(event.get("text") or ""))
        if tray is not None:
            tray.set_state("idle")
        return

    if kind == "busy":
        if getattr(hud, "mode", None) != "toast":
            hud.set_busy(str(event.get("message") or "处理中…"))
        if tray is not None:
            tray.set_state("busy")
        return

    if kind == "error":
        hud.set_error(str(event.get("message") or "操作失败"))
        if tray is not None:
            tray.set_state("error")
        return

    if kind == "history_write_failed":
        hud.show_toast(str(event.get("message") or "历史记录写入失败"), kind="warn", duration_ms=3000)
        return

    if kind == "delivery_unknown":
        job_id = str(event.get("job_id") or "")
        text = str(event.get("text") or "")
        if job_id and on_confirm_resend is not None:
            hud.show_delivery_confirmation(
                job_id,
                text,
                lambda jid, confirmed, _txt=text: on_confirm_resend(jid, _txt, confirmed),
            )
        else:
            hud.set_error("无法确认文字是否已输入，请检查后按重复键重试")
        if tray is not None:
            tray.set_state("error")


class ProductEventLoop:
    """消费产品 SSE 的后台守护循环：断线指数退避重连，收到有效事件即重置退避。"""

    def __init__(
        self,
        *,
        iter_events: Callable[[threading.Event], Iterator[dict[str, Any]]],
        on_event: Callable[[dict[str, Any]], None],
        backoff_initial_s: float = BACKOFF_INITIAL_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        wait_fn: Callable[[float], bool] | None = None,
    ) -> None:
        self._iter_events = iter_events
        self._on_event = on_event
        self._backoff_initial_s = max(0.01, float(backoff_initial_s))
        self._backoff_max_s = max(self._backoff_initial_s, float(backoff_max_s))
        self._stop = threading.Event()
        self._wait_fn = wait_fn or self._stop.wait
        self._thread: threading.Thread | None = None

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self.run_forever, name="product-events-sse", daemon=True)
        self._thread.start()
        return self._thread

    def run_forever(self) -> None:
        delay = self._backoff_initial_s
        while not self._stop.is_set():
            got_event = False
            try:
                for event in self._iter_events(self._stop):
                    if self._stop.is_set():
                        return
                    got_event = True
                    delay = self._backoff_initial_s
                    self._on_event(event)
            except Exception as exc:
                if self._stop.is_set():
                    return
                print(f"  事件流: 连接异常 ({exc})，{delay:.0f}s 后重连", flush=True)
            else:
                if self._stop.is_set():
                    return
                if not got_event:
                    print(f"  事件流: 服务端断开，{delay:.0f}s 后重连", flush=True)
            if self._wait_fn(delay):
                return
            delay = min(delay * 2.0, self._backoff_max_s)

    def request_stop(self) -> None:
        """先置停流标志，供调用方在关闭底层 HTTP/SSE 客户端前阻止重连与异常日志。"""
        self._stop.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
