"""Tests for settlement/accounting integrity bugs found 2026-06-11.

Root cause: mixed price conventions. Position.entry_price is stored in
side-cost terms (a NO position at 85¢ stores 85), but cost_basis,
unrealized_pnl, and close_position assumed YES-scale. Consequences:
- NO-side exposure undercounted ~6x (risk limits effectively bypassed)
- NO-side realized PnL inflated ~15x on wins
- Settled markets never marked finalized locally, so stale rows were
  re-scored and re-traded every cycle on 2-week-old snapshots.

Convention after fix: entry_price and current_price are ALWAYS in the
position's own side-cost terms. Settlement converts the YES-scale result
before computing PnL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.database import get_session, Base
from src.models.market import Market
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.portfolio.tracker import PortfolioTracker


def _mk_position(session, side, entry, qty, market_id="MKT-1"):
    pos = Position(
        market_id=market_id, side=side, entry_price=entry,
        quantity=qty, current_price=entry, status="open",
    )
    session.add(pos)
    return pos


# --- cost basis: entry is already side-cost, both sides identical formula ---

def test_no_cost_basis_uses_side_cost_entry(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        pos = _mk_position(session, "no", entry=85, qty=9)
        session.flush()
        # 9 contracts at 85¢ NO cost = $7.65 at risk, not (100-85)*9/100 = $1.35
        assert pos.cost_basis == 85 * 9 / 100


def test_yes_cost_basis_unchanged(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        pos = _mk_position(session, "yes", entry=40, qty=5)
        session.flush()
        assert pos.cost_basis == 40 * 5 / 100


# --- realized PnL on settlement ---

def test_no_position_win_pnl(db_engine):
    # NO at 94¢, market resolves NO (yes settles 0): profit = 6¢ x 3 = $0.18
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _mk_position(session, "no", entry=94, qty=3)
        session.commit()
    closed = PortfolioTracker(db_engine).close_position("MKT-1", exit_price=0)
    assert closed is not None
    assert abs(closed["realized_pnl"] - 0.18) < 1e-9


def test_no_position_loss_pnl(db_engine):
    # NO at 94¢, market resolves YES (yes settles 100): lose stake = -$2.82
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _mk_position(session, "no", entry=94, qty=3)
        session.commit()
    closed = PortfolioTracker(db_engine).close_position("MKT-1", exit_price=100)
    assert closed is not None
    assert abs(closed["realized_pnl"] - (-2.82)) < 1e-9


def test_yes_position_win_pnl(db_engine):
    # YES at 40¢ resolving YES: profit = 60¢ x 5 = $3.00
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _mk_position(session, "yes", entry=40, qty=5)
        session.commit()
    closed = PortfolioTracker(db_engine).close_position("MKT-1", exit_price=100)
    assert closed is not None
    assert abs(closed["realized_pnl"] - 3.00) < 1e-9


def test_yes_position_loss_pnl(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        _mk_position(session, "yes", entry=40, qty=5)
        session.commit()
    closed = PortfolioTracker(db_engine).close_position("MKT-1", exit_price=0)
    assert closed is not None
    assert abs(closed["realized_pnl"] - (-2.00)) < 1e-9


# --- unrealized PnL uses side-cost convention for both sides ---

def test_no_unrealized_pnl_side_terms(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        pos = _mk_position(session, "no", entry=85, qty=9)
        pos.current_price = 90  # NO price moved up — we profit
        session.flush()
        assert pos.unrealized_pnl == (90 - 85) * 9 / 100


# --- settled markets must be excluded from future scoring ---

def test_close_position_marks_market_finalized(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="MKT-1", title="t", category="Sports",
                           close_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
                           status="active"))
        _mk_position(session, "no", entry=94, qty=3)
        session.commit()
    PortfolioTracker(db_engine).close_position("MKT-1", exit_price=0, finalize_market=True)
    with get_session(db_engine) as session:
        mkt = session.query(Market).filter_by(market_id="MKT-1").one()
        assert mkt.status == "finalized"


# --- stale snapshots must not be scored ---

def test_scorer_skips_stale_snapshots(db_engine, monkeypatch):
    monkeypatch.setattr("src.ev.scorer.TRADE_PRICE_DERIVED_MODELS", True)
    from src.ev.scorer import score_all_markets
    Base.metadata.create_all(db_engine)
    stale_ts = datetime.now(timezone.utc) - timedelta(days=14)
    with get_session(db_engine) as session:
        session.add(Market(market_id="STALE-MKT", title="Will X happen?",
                           category="Economics",
                           close_date=datetime.now(timezone.utc) + timedelta(days=30),
                           status="active"))
        session.add(PriceSnapshot(market_id="STALE-MKT", yes_bid=13, yes_ask=16,
                                  last_price=15, volume=5000, timestamp=stale_ts))
        session.commit()
    results = score_all_markets(db_engine)
    assert all(r["market_id"] != "STALE-MKT" for r in results)


def test_scorer_accepts_fresh_snapshots(db_engine, monkeypatch):
    monkeypatch.setattr("src.ev.scorer.TRADE_PRICE_DERIVED_MODELS", True)
    from src.ev.scorer import score_all_markets
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="FRESH-MKT", title="Will X happen?",
                           category="Economics",
                           close_date=datetime.now(timezone.utc) + timedelta(days=30),
                           status="active"))
        session.add(PriceSnapshot(market_id="FRESH-MKT", yes_bid=64, yes_ask=66,
                                  last_price=65, volume=5000,
                                  timestamp=datetime.now(timezone.utc)))
        session.commit()
    results = score_all_markets(db_engine)
    assert any(r["market_id"] == "FRESH-MKT" for r in results)
