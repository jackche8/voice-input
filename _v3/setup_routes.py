"""设置页 API：真实状态检查、一键装齐、手机局域网入口签发。全部仅本机。"""
from __future__ import annotations

from fastapi import APIRouter

from _contracts import (
    ErrorCode,
    InstallAllStatus,
    LanAccessResponse,
    LogicError,
    SetupCheckResponse,
)
from _v3.runtime import Runtime


def build_setup_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["setup"])

    def _setup():
        if runtime.setup is None:
            raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入设置探测与安装动作")
        return runtime.setup

    @router.get("/api/setup/check", response_model=SetupCheckResponse)
    def setup_check() -> SetupCheckResponse:
        return _setup().check()

    @router.post("/api/setup/install-all", response_model=InstallAllStatus, status_code=202)
    def setup_install_all() -> InstallAllStatus:
        """开始（或从失败步重试）一键装齐；正在进行时原样返回当前状态，不重复启动。"""
        return _setup().sequence.start()

    @router.get("/api/v1/lan-access", response_model=LanAccessResponse)
    def lan_access() -> LanAccessResponse:
        if runtime.lan is None:
            raise LogicError(ErrorCode.dependency_failed, "当前运行时未注入局域网入口签发")
        return runtime.lan.describe()

    return router
