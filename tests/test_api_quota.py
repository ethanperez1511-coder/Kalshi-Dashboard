"""Contract test for GET /api/quota (Phase 1.2)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.quota import create_quota_router
from src.database import Base
from src.modeling.odds_store import OddsCacheStore, QuotaLedger, month_key
from datetime import datetime, timezone


def _client(engine):
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(create_quota_router(engine))
    return TestClient(app)


def test_quota_endpoint_reports_empty_state(db_engine):
    body = _client(db_engine).get("/api/quota").json()
    assert body["used"] == 0
    assert body["cap"] > 0
    assert body["remaining"] == body["cap"]
    assert body["days_to_exhaustion"] is None
    assert body["projected_overrun"] is False
    assert body["cache"] == []


def test_quota_endpoint_reports_spend_and_cache(db_engine):
    client = _client(db_engine)
    now = datetime.now(timezone.utc)
    QuotaLedger(db_engine, cap=500).charge(month_key(now), "the_odds_api", 42)
    OddsCacheStore(db_engine).put("baseball_mlb", "the_odds_api", [], now=now)

    body = client.get("/api/quota").json()
    assert body["used"] == 42
    assert body["remaining"] == body["cap"] - 42
    assert body["burn_per_day"] > 0
    assert [c["sport_key"] for c in body["cache"]] == ["baseball_mlb"]
    assert body["cache"][0]["age_minutes"] < 1.0
