"""Tests for system improvements (changes 1, 6, 9)."""
from __future__ import annotations
import math
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine

from src.database import get_engine, get_session, Base
from src.ev.calculator import kalshi_taker_fee, calculate_ev, fee_per_contract
from src.modeling.odds_api import devig_two_way, devig_book_then_average
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.backtest.runner import BacktestRunner
from src.backtest.models import BacktestRun, BacktestTrade


# ---------------------------------------------------------------------------
# Change 1: De-vig tests
# ---------------------------------------------------------------------------

class TestDeVig:
    def test_two_way_basic(self):
        """A two-way market implying 0.55/0.52 must de-vig to ~0.514/0.486."""
        yes, no = devig_two_way(0.55, 0.52)
        assert abs(yes + no - 1.0) < 1e-9, "Must sum to 1"
        assert abs(yes - 0.55 / 1.07) < 0.001
        assert abs(no - 0.52 / 1.07) < 0.001
        # Approx values
        assert abs(yes - 0.5140) < 0.001
        assert abs(no - 0.4860) < 0.001

    def test_two_way_no_vig(self):
        """Already fair market stays the same."""
        yes, no = devig_two_way(0.5, 0.5)
        assert abs(yes - 0.5) < 1e-9
        assert abs(no - 0.5) < 1e-9

    def test_two_way_zero(self):
        """Zero probs returns 50/50."""
        yes, no = devig_two_way(0, 0)
        assert yes == 0.5
        assert no == 0.5

    def test_book_then_average(self):
        """De-vig each book separately then average."""
        # Book 1: 0.55/0.52 (7% vig)
        # Book 2: 0.60/0.50 (10% vig)
        books = [[0.55, 0.52], [0.60, 0.50]]
        result = devig_book_then_average(books)
        assert abs(result[0] + result[1] - 1.0) < 0.01
        # Book 1 de-vigged: 0.514/0.486
        # Book 2 de-vigged: 0.545/0.455
        # Average: ~0.530/0.470
        assert abs(result[0] - 0.530) < 0.01

    def test_book_then_average_single_book(self):
        books = [[0.55, 0.52]]
        result = devig_book_then_average(books)
        assert abs(result[0] - 0.5140) < 0.001

    def test_book_then_average_empty(self):
        result = devig_book_then_average([])
        assert result == [0.5, 0.5]


# ---------------------------------------------------------------------------
# Change 6: Real fee formula tests
# ---------------------------------------------------------------------------

class TestKalshiFee:
    def test_fee_at_50_cents(self):
        """price=50 → fee = ceil(0.07 * 0.5 * 0.5 * 100) / 100 = ceil(1.75)/100 = 0.02"""
        fee = kalshi_taker_fee(50)
        assert fee == 0.02

    def test_fee_at_10_cents(self):
        """price=10 → fee = ceil(0.07 * 0.10 * 0.90 * 100) / 100 = ceil(0.63)/100 = 0.01"""
        fee = kalshi_taker_fee(10)
        assert fee == 0.01

    def test_fee_at_90_cents(self):
        """Symmetric with 10 cents."""
        fee = kalshi_taker_fee(90)
        assert fee == 0.01

    def test_fee_at_0(self):
        fee = kalshi_taker_fee(0)
        assert fee == 0.0

    def test_fee_at_100(self):
        fee = kalshi_taker_fee(100)
        assert fee == 0.0

    def test_maker_fee_is_zero(self):
        fee = fee_per_contract(50, order_type="maker")
        assert fee == 0.0

    def test_taker_fee_nonzero(self):
        fee = fee_per_contract(50, order_type="taker")
        assert fee == 0.02

    def test_ev_uses_real_fee(self):
        """EV calculation with real fee formula (no legacy fee_rate)."""
        result = calculate_ev(p_model=0.60, price_cents=50, order_type="maker")
        # Maker fee=0, so net_ev = raw_ev
        assert result.net_ev == result.raw_ev

    def test_ev_taker_has_fee(self):
        result = calculate_ev(p_model=0.60, price_cents=50, order_type="taker")
        assert result.net_ev < result.raw_ev

    def test_ev_legacy_fee_override(self):
        """Legacy fee_rate >= 0 overrides real formula."""
        result = calculate_ev(p_model=0.60, price_cents=50, fee_rate=0.05)
        expected_raw = 0.60 * 0.50 - 0.40 * 0.50
        assert abs(result.net_ev - (expected_raw - 0.05)) < 1e-9

    def test_ev_taker_crosses_spread(self):
        """Taker buys YES at ask price, not mid."""
        result = calculate_ev(
            p_model=0.60, price_cents=50,
            yes_bid=48, yes_ask=52,
            order_type="taker",
        )
        # YES fill at 52, not 50
        assert result.edge == 0.60 - 0.52  # edge based on ask


# ---------------------------------------------------------------------------
# Change 9: Backtest lookahead audit test
# ---------------------------------------------------------------------------

class TestBacktestLookahead:
    def test_no_future_snapshot_leaks(self, db_engine):
        """Backtest must not use snapshots at or after the decision time."""
        Base.metadata.create_all(db_engine)

        with get_session(db_engine) as session:
            session.add(Market(
                market_id="TEST-LOOK", title="Lookahead Test",
                category="General",
                close_date=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
                status="open",
            ))
            # Snapshot BEFORE close (should be used)
            session.add(PriceSnapshot(
                market_id="TEST-LOOK", yes_bid=60, yes_ask=64, last_price=62,
                volume=200,
                timestamp=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
            ))
            # Snapshot AT close time (should NOT be used as input)
            session.add(PriceSnapshot(
                market_id="TEST-LOOK", yes_bid=90, yes_ask=92, last_price=91,
                volume=300,
                timestamp=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
            ))
            # Snapshot AFTER close (for resolution)
            session.add(PriceSnapshot(
                market_id="TEST-LOOK", yes_bid=95, yes_ask=99, last_price=97,
                volume=400,
                timestamp=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
            ))
            session.commit()

        runner = BacktestRunner(db_engine)
        # Should not raise AssertionError
        run_id = runner.run(
            start_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
            initial_bankroll=100.0,
        )

        # Verify the backtest used pre-close data (62 cents, not 91)
        with get_session(db_engine) as session:
            trades = session.query(BacktestTrade).filter_by(run_id=run_id).all()
            for t in trades:
                if t.market_id == "TEST-LOOK":
                    # Entry price should be based on 62-cent snapshot, not 91
                    assert t.entry_price < 80, (
                        f"Lookahead detected: entry_price={t.entry_price} "
                        "suggests future data was used"
                    )
