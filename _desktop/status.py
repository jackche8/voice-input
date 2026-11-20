"""平台状态 → 产品事件流（/api/v1/events/platform）。后台串行发送，绝不阻塞 UI 主线程。

产品事件流（GET /api/v1/events）是状态胶囊与网页视图的唯一状态源；平台本地才知道的状态
（开麦失败、截图中、截图失败、没有可重复的记录）经这里进入同一事件流。
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from _contracts import ErrorCode


class PlatformStatus:
    def __init__(self, post: Callable[[dict], None], *, start_thread: bool = True) -> None:
        self._post = post
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._hotkey_lock = threading.Lock()
        self._hotkey_error: str | None = None
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="platform-status", daemon=True)
            self._thread.start()

    def busy(self) -> None:
        self._queue.put({"event": "busy"})

    def idle(self) -> None:
        self._queue.put({"event": "idle"})

    def error(self, code: ErrorCode, message: str) -> None:
        self._queue.put({"event": "error", "error": code.value, "message": message})

    def set_hotkey_error(self, message: str | None) -> None:
        """快捷键组件失败：记住原因并立即发一条 error；None = 已恢复，清除、不发。"""
        with self._hotkey_lock:
            self._hotkey_error = message
        if message is not None:
            self.error(ErrorCode.dependency_failed, message)

    def resend_hotkey_error(self) -> None:
        """事件流每次连上（服务端首条 idle）时调用：仍有快捷键组件失败就再发一条，保证底栏看得到。"""
        with self._hotkey_lock:
            message = self._hotkey_error
        if message is not None:
            self.error(ErrorCode.dependency_failed, message)

    def drain(self) -> None:
        """测试/退出用：同步发送已排队的事件。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._send(item)

    def close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._send(item)

    def _send(self, item: dict) -> None:
        try:
            self._post(item)
        except Exception as exc:
            # 服务不可达时状态无处可显示，只能记日志；业务结果仍以 job 状态为准。
            print(f"  状态: 上报失败 {item.get('event')}: {exc}", flush=True)
