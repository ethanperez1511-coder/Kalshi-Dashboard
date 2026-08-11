"""Checked-in match verdicts reach production (Phase 1.5).

The review queue is a database table, and the database that matters is Neon,
writable only from the cron runner. A ruling made while reading a report needs
a path into production; this is it.
"""
from __future__ import annotations

from src.database import Base, get_session
from src.modeling.match_seed import DECISIONS, SEED_SOURCE, apply_seed_decisions
from src.models.match_map import MarketMatchMap

SWIFT = "KXTAYLORSWIFTWEDDINGATTEND-28DEC31-MAX"


def _row(engine, market_id=SWIFT):
    """Detached snapshot — ORM instances expire when the session closes."""
    with get_session(engine) as s:
        row = s.query(MarketMatchMap).filter_by(kalshi_market_id=market_id).first()
        if row is None:
            return None
        return type("Snap", (), {
            "status": row.status,
            "decided_by": row.decided_by,
            "reason": row.reason or "",
            "poly_condition_id": row.poly_condition_id,
        })


def test_seeds_apply_to_an_empty_map(db_engine):
    Base.metadata.create_all(db_engine)
    applied = apply_seed_decisions(db_engine)
    assert f"{SWIFT}=blocked" in applied

    row = _row(db_engine)
    assert row.status == "blocked"
    assert row.decided_by == SEED_SOURCE
    assert "virtual attendance" in row.reason


def test_every_decision_applies(db_engine):
    Base.metadata.create_all(db_engine)
    applied = apply_seed_decisions(db_engine)
    assert len(applied) == len(DECISIONS)
    assert "KXNEXTISRAELPM-45JAN01-GEIS=blocked" in applied


def test_is_idempotent(db_engine):
    Base.metadata.create_all(db_engine)
    apply_seed_decisions(db_engine)
    assert apply_seed_decisions(db_engine) == []
    with get_session(db_engine) as s:
        assert s.query(MarketMatchMap).filter_by(kalshi_market_id=SWIFT).count() == 1


def test_upgrades_a_pending_row(db_engine):
    """A pair auto-queued by the entity check gets the human ruling applied."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(MarketMatchMap(
            kalshi_market_id=SWIFT, poly_condition_id="0xold",
            status="pending", similarity=0.8,
        ))
        s.commit()
    apply_seed_decisions(db_engine)
    assert _row(db_engine).status == "blocked"


def test_never_overrides_a_dashboard_decision(db_engine):
    """If the operator approves it in the UI, the checked-in verdict yields."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(MarketMatchMap(
            kalshi_market_id=SWIFT, poly_condition_id="0xabc",
            status="approved", similarity=1.0, decided_by="human",
        ))
        s.commit()
    applied = apply_seed_decisions(db_engine)
    assert not any(a.startswith(SWIFT) for a in applied)
    assert _row(db_engine).status == "approved"


def test_blocked_pair_yields_no_estimate(db_engine):
    """End to end: a seeded block actually silences the model."""
    from src.modeling.models.polymarket import PolymarketModel
    from src.modeling.polymarket_api import PolyMarket

    Base.metadata.create_all(db_engine)
    apply_seed_decisions(db_engine)

    seeded = DECISIONS[0]
    client = type("C", (), {"get_markets": lambda self: [PolyMarket(
        question=seeded.poly_question, yes_price=0.68, volume_usd=2_000_000.0,
        condition_id=seeded.poly_condition_id,
    )]})()

    assert PolymarketModel(client).estimate(
        SWIFT, seeded.kalshi_title, 0.5, db_engine,
    ) is None
