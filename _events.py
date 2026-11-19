"""产品事件派发。只接受已冻结的产品 SSE 模型，不做鉴权，不虚构成功或 unknown 记录。"""
from __future__ import annotations

import threading
from collections.abc import Callable

from _contracts import ProductSSEEvent, ProductSSEEventEnvelope

Listener = Callable[[ProductSSEEvent], None]


class EventBus:
    """进程内线程安全派发。HTTP SSE 路由属于后续集成，本层只提供订阅与发布。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def publish(self, event: ProductSSEEvent) -> ProductSSEEvent:
        wrapped = ProductSSEEventEnvelope.model_validate(
            event.model_dump() if hasattr(event, "model_dump") else event
        )
        payload = wrapped.root
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener(payload)
        return payload

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item is not listener]
