from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.database import get_session, Base
from src.modeling.models.sports import SportsModel
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


def _add_snapshots(
    session,
    market_id: str,
    prices: list,
) -> None:
    for i, price in enumerate(prices):
        bid = price - 1
        ask = price + 1
        session.add(
            PriceSnapshot(
                market_id=market_id,
                yes_bid=bid,
                yes_ask=ask,
                last_price=price,
                volume=500,
                timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            )
        )


# ---------------------------------------------------------------------------
# test_sports_model_category
# ---------------------------------------------------------------------------

def test_sports_model_category():
    model = SportsModel()
    assert model.category == "Sports"


# ---------------------------------------------------------------------------
# test_sports_model_estimates_game_market
# ---------------------------------------------------------------------------

def test_sports_model_estimates_game_market(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "NBA-FINALS-2026", "Will the Lakers win Game 7?", "Sports")
        _add_snapshots(session, "NBA-FINALS-2026", prices=[60, 62, 61, 63, 62, 64, 63, 65, 64, 66])
        session.commit()

    model = SportsModel()
    result = model.estimate(
        "NBA-FINALS-2026",
        "Will the Lakers win Game 7?",
        current_price=63,
        engine=db_engine,
    )

    assert result is not None
    assert 0.0 <= result.p_model <= 1.0
    assert result.confidence > 0.0


# ---------------------------------------------------------------------------
# test_sports_model_skips_non_sports
# ---------------------------------------------------------------------------

def test_sports_model_skips_non_sports(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "FED-SKIP", "Will the Fed cut rates?", "Economics")
        _add_snapshots(session, "FED-SKIP", prices=[50, 51, 52, 53, 54, 55, 56, 57, 58, 59])
        session.commit()

    model = SportsModel()
    result = model.estimate(
        "FED-SKIP",
        "Will the Fed cut rates?",
        current_price=55,
        engine=db_engine,
    )

    assert result is None


# ---------------------------------------------------------------------------
# test_sports_model_adjusts_for_volatility
# ---------------------------------------------------------------------------

def test_sports_model_adjusts_for_volatility(db_engine):
    """Highly volatile prices should yield a confidence score below 0.6."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "VOLATILE-GAME", "Will Team X win?", "Sports")
        # Wide swings: alternates between 10 and 90 → very high volatility
        volatile_prices = [10, 90, 15, 85, 20, 80, 25, 75, 30, 70]
        _add_snapshots(session, "VOLATILE-GAME", prices=volatile_prices)
        session.commit()

    model = SportsModel()
    result = model.estimate(
        "VOLATILE-GAME",
        "Will Team X win?",
        current_price=50,
        engine=db_engine,
    )

    assert result is not None
    assert result.confidence < 0.6


# ---------------------------------------------------------------------------
# test_sports_model_returns_none_with_too_few_snapshots
# ---------------------------------------------------------------------------

def test_sports_model_returns_none_with_too_few_snapshots(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "SPORTS-SPARSE", "Will Team Y win?", "Sports")
        _add_snapshots(session, "SPORTS-SPARSE", prices=[55, 56, 57])
        session.commit()

    model = SportsModel()
    result = model.estimate(
        "SPORTS-SPARSE",
        "Will Team Y win?",
        current_price=56,
        engine=db_engine,
    )

    assert result is None


# ---------------------------------------------------------------------------
# test_sports_model_mean_reversion_for_high_price
# ---------------------------------------------------------------------------

def test_sports_model_mean_reversion_for_high_price(db_engine):
    """Prices consistently above 0.8 should be pulled slightly toward 0.5."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _add_market(session, "HIGH-PROB-GAME", "Will heavy favourite win?", "Sports")
        # All prices near 88-92 (yes_bid = price-1, yes_ask = price+1)
        high_prices = [88, 89, 90, 91, 92, 88, 89, 90, 91, 92]
        _add_snapshots(session, "HIGH-PROB-GAME", prices=high_prices)
        session.commit()

    model = SportsModel()
    result = model.estimate(
        "HIGH-PROB-GAME",
        "Will heavy favourite win?",
        current_price=90,
        engine=db_engine,
    )

    assert result is not None
    # Mean reversion: p_model should be strictly less than the raw midpoint avg (~0.90)
    raw_midpoint_avg = 0.90
    assert result.p_model < raw_midpoint_avg
