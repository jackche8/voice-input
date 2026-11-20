"""v3 HTTP 组合：显式 Runtime + 按职责拆分的路由 + static/v3 唯一界面。"""

from _v3.production import compose_production_runtime
from _v3.runtime import Runtime, build_sandbox_runtime

__all__ = [
    "Runtime",
    "build_sandbox_runtime",
    "compose_production_runtime",
]
