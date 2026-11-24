"""FastAPI 入口：接收让出方与划转量，查询两组配额与最近一次处理结果。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .service import (
    RecoveryPendingError,
    TransferError,
    TransferService,
    WriteFailureError,
)

log = logging.getLogger("quota-transfer")


def build_service_from_env() -> TransferService:
    crash_hook = None
    if os.environ.get("CRASH_AFTER_DONOR_WRITE") == "1":
        # 演示用：模拟“首个组写完后、第二个组写之前进程崩溃退出”
        # （划转场景为让出方降额后；周期切换场景为第一组切换后）
        def crash_hook(_stage: str) -> None:
            os._exit(42)

    target_period_raw = os.environ.get("CPU_PERIOD_USEC", "").strip()
    if target_period_raw:
        try:
            target_period = int(target_period_raw)
        except ValueError:
            raise ValueError(
                f"CPU_PERIOD_USEC 必须为正整数（微秒），收到: {target_period_raw!r}"
            )
        if target_period <= 0:
            raise ValueError(
                f"CPU_PERIOD_USEC 必须为正整数（微秒），收到: {target_period_raw!r}"
            )
    else:
        target_period = None

    return TransferService(
        group_paths={
            "groupA": Path(os.environ.get("GROUP_A_CPU_MAX", "/cgroup/groupA/cpu.max")),
            "groupB": Path(os.environ.get("GROUP_B_CPU_MAX", "/cgroup/groupB/cpu.max")),
        },
        state_dir=Path(os.environ.get("STATE_DIR", "/state")),
        min_quota=int(os.environ.get("MIN_QUOTA", "20000")),
        crash_hook=crash_hook,
        target_period=target_period,
    )


class TransferRequest(BaseModel):
    donor: str = Field(description="让出方组名（groupA 或 groupB）")
    amount: int = Field(
        gt=0,
        description=(
            "划转量，始终按首次周期切换前的基线周期核算"
            "（cpu.max quota 单位，微秒/基线周期）"
        ),
    )


class TransferResponse(BaseModel):
    action: str
    result: str
    transfer_id: str
    donor: str
    receiver: str
    amount: int
    amount_period: int | None = None
    applied_delta: int | None = None
    active_period: int | None = None
    post: dict
    at: str


def create_app(service: TransferService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时先恢复：回退未登记成功的转移，保留已登记成功的新配额。
        # 恢复完成前（含恢复失败）recovered=False，新转移一律被拒绝。
        try:
            result = service.recover()
            log.info("startup recovery: %s", result)
        except Exception:
            log.exception("启动恢复失败；恢复完成前将拒绝新转移")
        yield

    app = FastAPI(title="CPU Quota Transfer Service", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/quotas")
    def get_quotas() -> dict:
        """查询两组当前配额。"""
        return service.quotas()

    @app.get("/status")
    def get_status() -> dict:
        """查询恢复状态与最近一次处理结果。"""
        return service.status()

    @app.post("/transfers", response_model=TransferResponse)
    def post_transfer(req: TransferRequest) -> dict:
        """发起一笔划转：donor 组让出 amount 给另一组。"""
        try:
            return service.transfer(req.donor, req.amount)
        except RecoveryPendingError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except TransferError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except WriteFailureError as e:
            raise HTTPException(status_code=500, detail=str(e))

    return app


def app_factory() -> FastAPI:
    """uvicorn --factory 入口：延迟到真正启动时才读取环境、触碰文件系统。"""
    return create_app(build_service_from_env())
