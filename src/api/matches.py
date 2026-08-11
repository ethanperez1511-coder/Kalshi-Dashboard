"""Review queue for uncertain Kalshi↔Polymarket mappings.

Anything the entity comparison cannot affirm produces no estimate and lands
here. Approving a pair once records it permanently, so the fail-closed policy
costs coverage only until the queue is worked.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from src.modeling.match_store import decide, list_matches


class MatchResponse(BaseModel):
    id: int
    kalshi_market_id: str
    poly_condition_id: str
    status: str
    similarity: float
    kalshi_title: Optional[str]
    poly_question: Optional[str]
    verdict: Optional[str]
    reason: Optional[str]
    decided_by: Optional[str]
    created_at: Optional[str]


def create_matches_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/matches", tags=["matches"])

    @router.get("", response_model=List[MatchResponse])
    def get_matches(status: Optional[str] = None):
        return [MatchResponse(**m) for m in list_matches(engine, status)]

    @router.get("/pending", response_model=List[MatchResponse])
    def get_pending():
        return [MatchResponse(**m) for m in list_matches(engine, "pending")]

    @router.post("/{match_id}/approve", response_model=MatchResponse)
    def approve(match_id: int):
        return _decide_or_404(match_id, "approved")

    @router.post("/{match_id}/block", response_model=MatchResponse)
    def block(match_id: int):
        return _decide_or_404(match_id, "blocked")

    def _decide_or_404(match_id: int, status: str) -> MatchResponse:
        if not decide(engine, match_id, status):
            raise HTTPException(status_code=404, detail=f"no match {match_id}")
        row = next(m for m in list_matches(engine) if m["id"] == match_id)
        return MatchResponse(**row)

    return router
