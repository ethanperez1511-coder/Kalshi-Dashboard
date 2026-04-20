from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.database import get_session, Base
from src.modeling.models.finance import FinanceModel
from src.models.market import Market
from src.models.price import PriceSnapshot


def _add_market(session, market_id: str, title: str, category: str) -> None:
    session.add(
        Market(
            market_id=market_id,
            title=title,
            category=category,
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
            status="open",
        )
    )


def _add_snapshots(session, market_id: str, bid: int, ask: int, count: int = 10) -> None:
    for i in range(count):
        session.add(
            PriceSnapshot(
                market_id=market_id,
                yes_bid=bid,
                yes_ask=ask,
                last_price=(bid + ask) // 2,
                volume=1000,
                timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            )
        )


# ---------------------------------------------------------------------------
# test_finance_model_category
# ---------------------------------------------------------------------------

def test_finance_model_category():
    model = FinanceModel()
    assert model.category == "Economics"


# ---------------------------------------------------------------------------
# test_finance_model_estimates_fed_market
# ---------------------------------------------------------------------------

def test_finance_model_estimates_fed_market(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "FED-RATE-2026", "Will the Fed raise rates in 2026?", "Economics")
        _add_snapshots(session, "FED-RATE-2026", bid=65, ask=67)
        session.commit()

    model = FinanceModel()
    result = model.estimate(
        "FED-RATE-2026",
        "Will the Fed raise rates in 2026?",
        current_price=66,
        engine=db_engine,
    )

    assert result is not None
    assert 0.0 <= result.p_model <= 1.0
    assert result.confidence > 0.0


# ---------------------------------------------------------------------------
# test_finance_model_skips_non_finance
# ---------------------------------------------------------------------------

def test_finance_model_skips_non_finance(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "SPORTS-MKT", "Will the Lakers win tonight?", "Sports")
        _add_snapshots(session, "SPORTS-MKT", bid=60, ask=62)
        session.commit()

    model = FinanceModel()
    result = model.estimate(
        "SPORTS-MKT",
        "Will the Lakers win tonight?",
        current_price=61,
        engine=db_engine,
    )

    assert result is None


# ---------------------------------------------------------------------------
# test_finance_model_adjusts_for_keywords
# ---------------------------------------------------------------------------

def test_finance_model_adjusts_for_keywords(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(
            session,
            "CPI-2026",
            "Will CPI inflation exceed 3% in Q3 2026?",
            "Economics",
        )
        _add_snapshots(session, "CPI-2026", bid=50, ask=52)
        session.commit()

    model = FinanceModel()
    result = model.estimate(
        "CPI-2026",
        "Will CPI inflation exceed 3% in Q3 2026?",
        current_price=51,
        engine=db_engine,
    )

    assert result is not None
    reasoning_lower = result.reasoning.lower()
    assert "cpi" in reasoning_lower or "inflation" in reasoning_lower


# ---------------------------------------------------------------------------
# test_finance_model_returns_none_with_too_few_snapshots
# ---------------------------------------------------------------------------

def test_finance_model_returns_none_with_too_few_snapshots(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "FED-SPARSE", "Will the Fed cut rates?", "Economics")
        _add_snapshots(session, "FED-SPARSE", bid=55, ask=57, count=3)
        session.commit()

    model = FinanceModel()
    result = model.estimate(
        "FED-SPARSE",
        "Will the Fed cut rates?",
        current_price=56,
        engine=db_engine,
    )

    assert result is None


# ---------------------------------------------------------------------------
# test_finance_model_no_keyword_uses_direct_consensus
# ---------------------------------------------------------------------------

def test_finance_model_no_keyword_uses_direct_consensus(db_engine):
    """A market with no matching keywords should use market consensus directly
    and have lower data_completeness (0.4 path)."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(
            session,
            "ECON-GENERIC",
            "Will the economy do well?",
            "Economics",
        )
        _add_snapshots(session, "ECON-GENERIC", bid=70, ask=72)
        session.commit()

    model = FinanceModel()
    result = model.estimate(
        "ECON-GENERIC",
        "Will the economy do well?",
        current_price=71,
        engine=db_engine,
    )

    assert result is not None
    # No keyword blend; result should be close to market midpoint (71/100 = 0.71)
    assert 0.65 <= result.p_model <= 0.77
