from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.models.trade import Trade
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.metrics import compute_metrics
from src.portfolio.equity import build_equity_curve


class PositionResponse(BaseModel):
    market_id: str
    side: str
    entry_price: int
    current_price: int
    quantity: int
    unrealized_pnl: float
    cost_basis: float
    opened_at: Optional[str]


class TradeResponse(BaseModel):
    market_id: str
    side: str
    action: str
    price: int
    quantity: int
    p_model: float
    implied_prob: float
    edge: float
    net_ev: float
    position_size_dollars: float
    confidence: float
    reasoning: str
    is_paper: bool
    status: str
    exit_price: Optional[int]
    realized_pnl: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True


def create_portfolio_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
    tracker = PortfolioTracker(engine)

    @router.get("/summary")
    def get_summary():
        return tracker.get_summary()

    @router.get("/positions", response_model=List[PositionResponse])
    def get_positions():
        return tracker.get_open_positions()

    @router.get("/trades", response_model=List[TradeResponse])
    def get_trades(status: Optional[str] = None, limit: int = 100):
        with get_session(engine) as session:
            query = session.query(Trade)
            if status:
                query = query.filter(Trade.status == status)
            trades = query.order_by(Trade.created_at.desc()).limit(limit).all()
            return [TradeResponse.model_validate(t) for t in trades]

    @router.get("/metrics")
    def get_metrics():
        return compute_metrics(engine)

    @router.get("/equity")
    def get_equity():
        return build_equity_curve(engine)

    return router
