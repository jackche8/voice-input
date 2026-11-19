"""job 查询 / 本机 claim / ack。投递状态只以 jobs 为准。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from _contracts import (
    JobAckAccepted,
    JobAckRequest,
    JobClaimRequest,
    JobClaimed,
    JobQueryFound,
    JobQueryUnknown,
)
from _v3.runtime import Runtime

# 长轮询上限 25s；由底层 JobStore Condition 零轮询唤醒，无任务返回 null。不重投、不编造 job。
CLAIM_WAIT_MAX_S = 25.0


def build_jobs_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["jobs"])

    @router.post("/api/v1/jobs/claim", response_model=JobClaimed | None)
    async def claim(body: JobClaimRequest | None = None) -> JobClaimed | None:
        wait = min(body.timeout_s or 0.0, CLAIM_WAIT_MAX_S) if body is not None else 0.0
        return await asyncio.to_thread(runtime.jobs.claim, wait_s=wait)

    @router.post("/api/v1/jobs/{job_id}/ack", response_model=JobAckAccepted)
    async def ack(job_id: str, body: JobAckRequest) -> JobAckAccepted:
        record = await asyncio.to_thread(runtime.jobs.ack, job_id, body)
        return JobAckAccepted(job_id=record.job_id, state=record.state)

    @router.get("/api/v1/jobs/{job_id}", response_model=JobQueryFound | JobQueryUnknown)
    async def query(job_id: str) -> JobQueryFound | JobQueryUnknown:
        return await asyncio.to_thread(runtime.jobs.get, job_id)

    return router
