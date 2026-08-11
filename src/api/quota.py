"""Odds-provider quota status for the dashboard.

The metered odds provider is the one resource that can quietly kill sports
coverage for a whole month, and the failure is invisible from the outside — the
pipeline keeps running, the model just stops producing estimates. This endpoint
exists so the burn rate is on screen before that happens rather than after.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine

from src.modeling.odds_store import cache_status, quota_snapshot
from src.trading_config import ENABLE_ESPN_ODDS, ODDS_MONTHLY_QUOTA


class CacheEntryResponse(BaseModel):
    sport_key: str
    source: str
    fetched_at: str
    age_minutes: float


class QuotaResponse(BaseModel):
    month: str
    source: str
    used: int
    cap: int
    remaining: int
    burn_per_day: float
    projected_month_end: float
    days_in_month: int
    days_elapsed: float
    days_to_exhaustion: Optional[float]
    projected_overrun: bool
    fallback_enabled: bool
    cache: List[CacheEntryResponse]


def create_quota_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/quota", tags=["quota"])

    @router.get("", response_model=QuotaResponse)
    def get_quota():
        snap = quota_snapshot(engine, cap=ODDS_MONTHLY_QUOTA)
        snap["fallback_enabled"] = ENABLE_ESPN_ODDS
        snap["cache"] = cache_status(engine)
        return QuotaResponse(**snap)

    return router
