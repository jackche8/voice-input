"""生产 Web 入口（薄）：uvicorn --factory app:create_production_app（由应用主进程的看护层拉起）。

唯一装配在 _v3.production.build_production_app：v1 API + static/v3 界面 + 统一鉴权。
导入本模块没有副作用；只有 uvicorn 调用工厂时才绑定 live 数据与令牌。
"""
from __future__ import annotations

import os


def create_production_app():
    from _v3.production import build_production_app

    app, _paths = build_production_app(environ=os.environ)
    return app
