from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.models.market import Market
from src.models.trade import Trade
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.metrics import compute_metrics
from src.portfolio.equity import build_equity_curve


class PositionResponse(BaseModel):
    market_id: str
    title: str = ""
    side: str
    entry_price: int
    current_price: int
    quantity: int
    unrealized_pnl: float
    cost_basis: float
    opened_at: Optional[str]


class TradeResponse(BaseModel):
    market_id: str
    title: str = ""
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
        positions = tracker.get_open_positions()
        with get_session(engine) as session:
            market_ids = [p["market_id"] for p in positions]
            titles = {
                m.market_id: m.title
                for m in session.query(Market).filter(Market.market_id.in_(market_ids)).all()
            }
            for p in positions:
                p["title"] = titles.get(p["market_id"], p["market_id"])
        return positions

    @router.get("/trades", response_model=List[TradeResponse])
    def get_trades(status: Optional[str] = None, limit: int = 100):
        with get_session(engine) as session:
            query = session.query(Trade)
            if status:
                query = query.filter(Trade.status == status)
            trades = query.order_by(Trade.created_at.desc()).limit(limit).all()
            market_ids = [t.market_id for t in trades]
            titles = {
                m.market_id: m.title
                for m in session.query(Market).filter(Market.market_id.in_(market_ids)).all()
            }
            results = []
            for t in trades:
                resp = TradeResponse.model_validate(t)
                resp.title = titles.get(t.market_id, t.market_id)
                results.append(resp)
            return results

    @router.get("/metrics")
    def get_metrics():
        return compute_metrics(engine)

    @router.get("/equity")
    def get_equity():
        return build_equity_curve(engine)

    return router
