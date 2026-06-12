from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.ev.scorer import score_all_markets
from src.models.opportunity import Opportunity


class OpportunityResponse(BaseModel):
    market_id: str
    p_model: float
    implied_prob: float
    edge: float
    net_ev: float
    recommended_side: str
    confidence: float
    status: str
    reasoning: Optional[str]
    model_name: str

    class Config:
        from_attributes = True


def create_trading_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/trading", tags=["trading"])

    @router.get("/opportunities", response_model=List[OpportunityResponse])
    def get_opportunities(status: Optional[str] = None):
        with get_session(engine) as session:
            query = session.query(Opportunity)
            if status:
                query = query.filter(Opportunity.status == status)
            opps = query.order_by(Opportunity.net_ev.desc()).all()
            return [OpportunityResponse.model_validate(o) for o in opps]

    @router.post("/score")
    def trigger_scoring():
        results = score_all_markets(engine)
        qualifying = sum(1 for r in results if r["status"] == "qualifying")
        watching = sum(1 for r in results if r["status"] == "watching")
        return {
            "scored": len(results),
            "qualifying": qualifying,
            "watching": watching,
            "rejected": len(results) - qualifying - watching,
        }

    return router
