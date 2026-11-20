"""看护层组件状态：应用主进程整份上报、服务端只缓存最新一份；设置页/托盘的「重试」经产品事件流转给应用。全部仅本机。"""
from __future__ import annotations

import threading

from fastapi import APIRouter

from _contracts import (
    ComponentName,
    ComponentReport,
    ComponentReportAccepted,
    ComponentRetryAccepted,
    ComponentStatus,
    ErrorCode,
    LogicError,
    ProductComponentCommandEvent,
)
from _events import EventBus


class ComponentRegistry:
    """最新一份组件快照（整份替换）。服务端重启后为空，由应用在事件流重连时重报重建。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._components: list[ComponentStatus] = []

    def replace(self, report: ComponentReport) -> None:
        with self._lock:
            self._components = list(report.components)

    def snapshot(self) -> list[ComponentStatus]:
        with self._lock:
            return list(self._components)

    def get(self, name: ComponentName) -> ComponentStatus | None:
        return next((item for item in self.snapshot() if item.name == name), None)


def build_component_router(registry: ComponentRegistry, events: EventBus) -> APIRouter:
    router = APIRouter(tags=["components"])

    @router.post("/api/v1/components", response_model=ComponentReportAccepted, status_code=202)
    def report_components(body: ComponentReport) -> ComponentReportAccepted:
        registry.replace(body)
        return ComponentReportAccepted()

    @router.post("/api/v1/components/{name}/retry", response_model=ComponentRetryAccepted, status_code=202)
    def retry_component(name: str) -> ComponentRetryAccepted:
        if name not in ComponentName.__members__:
            raise LogicError(ErrorCode.not_found, f"没有组件 {name!r}", {"known": [n.value for n in ComponentName]})
        events.publish(ProductComponentCommandEvent(action="retry", component=ComponentName(name)))
        return ComponentRetryAccepted()

    return router


__all__ = ["ComponentRegistry", "build_component_router"]
