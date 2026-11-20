"""平台状态 → 服务端：产品事件（/api/v1/events/platform）与组件快照（/api/v1/components）。后台串行发送，绝不阻塞 UI 主线程。

产品事件流（GET /api/v1/events）是状态胶囊与网页视图的唯一状态源；平台本地才知道的失败
（开麦失败、截图失败、没有可重复的记录、快捷键组件失败、Ollama/识别服务停止）经这里进入同一事件流。
看护层的组件快照只存最新一份；事件流每次连上（服务端可能刚重启、缓存为空）时与快捷键错误一起重报。
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from _contracts import ComponentReport, ComponentStatus, ErrorCode

Post = Callable[[dict], None]


class PlatformStatus:
    def __init__(self, post: Post, *, post_components: Post, start_thread: bool = True) -> None:
        self._post = post
        self._post_components = post_components
        self._queue: queue.Queue[tuple[Post, dict] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._hotkey_error: str | None = None
        self._components: list[ComponentStatus] | None = None
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="platform-status", daemon=True)
            self._thread.start()

    def error(self, code: ErrorCode, message: str) -> None:
        self._queue.put((self._post, {"event": "error", "error": code.value, "message": message}))

    def set_components(self, components: list[ComponentStatus]) -> None:
        """看护层快照变化：记住最新一份并上报（整份替换）。"""
        with self._lock:
            self._components = list(components)
        self._queue.put((self._post_components, ComponentReport(components=components).model_dump(mode="json")))

    def set_hotkey_error(self, message: str | None) -> None:
        """快捷键组件失败：记住原因并立即发一条 error；None = 已恢复，清除、不发。"""
        with self._lock:
            self._hotkey_error = message
        if message is not None:
            self.error(ErrorCode.dependency_failed, message)

    def resend_on_connect(self) -> None:
        """事件流每次连上（服务端首条 idle）时调用：仍有快捷键组件失败就再发一条；有组件快照就重报（服务端缓存可能已随重启清空）。
        不重发历史上的组件停止 error，免得每次重连都弹一遍。"""
        with self._lock:
            message = self._hotkey_error
            components = self._components
        if message is not None:
            self.error(ErrorCode.dependency_failed, message)
        if components is not None:
            self._queue.put((self._post_components, ComponentReport(components=components).model_dump(mode="json")))

    def drain(self) -> None:
        """测试/退出用：同步发送已排队的事件。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._send(*item)

    def close(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._send(*item)

    def _send(self, post: Post, item: dict) -> None:
        try:
            post(item)
        except Exception as exc:
            # 服务不可达时状态无处可显示，只能记日志；组件快照在事件流重连时重报，业务结果仍以 job 状态为准。
            print(f"  状态: 上报失败 {item.get('event', '组件快照')}: {exc}", flush=True)
