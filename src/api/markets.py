from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.models.market import Market
from src.models.price import PriceSnapshot


class MarketResponse(BaseModel):
    market_id: str
    title: str
    category: str
    sub_category: Optional[str]
    close_date: datetime
    status: str
    rules: Optional[str]

    class Config:
        from_attributes = True


class PriceResponse(BaseModel):
    yes_bid: int
    yes_ask: int
    last_price: int
    volume: int
    timestamp: datetime

    class Config:
        from_attributes = True


def create_markets_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/markets", tags=["markets"])

    @router.get("", response_model=List[MarketResponse])
    def list_markets(category: Optional[str] = None, status: Optional[str] = None):
        with get_session(engine) as session:
            query = session.query(Market)
            if category:
                query = query.filter(Market.category == category)
            if status:
                query = query.filter(Market.status == status)
            markets = query.order_by(Market.close_date).all()
            return [MarketResponse.model_validate(m) for m in markets]

    @router.get("/{market_id}", response_model=MarketResponse)
    def get_market(market_id: str):
        with get_session(engine) as session:
            market = session.query(Market).filter_by(market_id=market_id).first()
            if not market:
                raise HTTPException(status_code=404, detail="Market not found")
            return MarketResponse.model_validate(market)

    @router.get("/{market_id}/prices", response_model=List[PriceResponse])
    def get_price_history(market_id: str, limit: int = 100):
        with get_session(engine) as session:
            market = session.query(Market).filter_by(market_id=market_id).first()
            if not market:
                raise HTTPException(status_code=404, detail="Market not found")
            snapshots = (
                session.query(PriceSnapshot)
                .filter_by(market_id=market_id)
                .order_by(PriceSnapshot.timestamp)
                .limit(limit)
                .all()
            )
            return [PriceResponse.model_validate(s) for s in snapshots]

    return router
