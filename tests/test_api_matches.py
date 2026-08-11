"""Contract test for the match review queue (Phase 1.3)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.matches import create_matches_router
from src.database import Base, get_session
from src.models.match_map import MarketMatchMap


def _client(engine):
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(create_matches_router(engine))
    return TestClient(app)


def _pending(engine, market_id="KX-1", cid="0xabc"):
    with get_session(engine) as s:
        row = MarketMatchMap(
            kalshi_market_id=market_id, poly_condition_id=cid, status="pending",
            similarity=0.82, kalshi_title="Kalshi above 3%",
            poly_question="Poly below 3%", verdict="conflict",
            reason="threshold direction differs",
        )
        s.add(row)
        s.commit()
        return row.id


def test_pending_queue_lists_uncertain_pairs(db_engine):
    client = _client(db_engine)
    _pending(db_engine)
    body = client.get("/api/matches/pending").json()
    assert len(body) == 1
    assert body[0]["verdict"] == "conflict"
    assert body[0]["reason"] == "threshold direction differs"
    assert body[0]["kalshi_title"] == "Kalshi above 3%"


def test_approve_marks_human_decision(db_engine):
    client = _client(db_engine)
    match_id = _pending(db_engine)
    body = client.post(f"/api/matches/{match_id}/approve").json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "human"
    assert client.get("/api/matches/pending").json() == []


def test_block_marks_human_decision(db_engine):
    client = _client(db_engine)
    match_id = _pending(db_engine)
    assert client.post(f"/api/matches/{match_id}/block").json()["status"] == "blocked"
    assert client.get("/api/matches/pending").json() == []


def test_unknown_match_is_404(db_engine):
    assert _client(db_engine).post("/api/matches/999/approve").status_code == 404
