from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.backtest.models import BacktestRun
from src.backtest.runner import BacktestRunner
from src.backtest.report import build_report


class BacktestRequest(BaseModel):
    start_date: datetime
    end_date: datetime
    initial_bankroll: float = 100.0
    category_filter: Optional[str] = None


class BacktestRunSummary(BaseModel):
    id: int
    start_date: datetime
    end_date: datetime
    initial_bankroll: float
    final_bankroll: float
    total_trades: int
    total_pnl: float
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


def create_backtest_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/backtest", tags=["backtest"])

    @router.post("/run")
    def run_backtest(req: BacktestRequest):
        runner = BacktestRunner(engine)
        run_id = runner.run(
            start_date=req.start_date,
            end_date=req.end_date,
            initial_bankroll=req.initial_bankroll,
            category_filter=req.category_filter,
        )
        with get_session(engine) as session:
            run = session.query(BacktestRun).get(run_id)
            return {
                "run_id": run.id,
                "status": run.status,
                "total_trades": run.total_trades,
                "total_pnl": run.total_pnl,
                "final_bankroll": run.final_bankroll,
            }

    @router.get("/{run_id}")
    def get_report(run_id: int):
        report = build_report(engine, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Backtest run not found")
        return report

    @router.get("", response_model=List[BacktestRunSummary])
    def list_runs(limit: int = 20):
        with get_session(engine) as session:
            runs = (
                session.query(BacktestRun)
                .order_by(BacktestRun.created_at.desc())
                .limit(limit)
                .all()
            )
            return [BacktestRunSummary.model_validate(r) for r in runs]

    return router
