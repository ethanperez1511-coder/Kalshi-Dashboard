"""Tests for pre-live filter hardening: disagreement cap, YES longshot skip,
conservative paper fills.

Context: 36 settled paper trades showed YES longshot buys with huge claimed
edges (avg +29%) losing money, while NO-side trades won. Large model-market
disagreement signals bad data, not alpha.
"""
from __future__ import annotations

from src.ev.calculator import EVResult
from src.ev.filter import TradeFilter
from src.risk.manager import TradeDecision
from src.trading.engine import TradeEngine


def _ev(p_model, implied, side):
    edge = p_model - implied
    return EVResult(
        p_model=p_model, implied_prob=implied,
        edge=edge, no_edge=-edge,
        raw_ev=edge, net_ev=edge - 0.01,
        no_ev=-edge - 0.01,
        recommended_side=side, fee_rate=0.01,
    )


# --- 1. Disagreement cap ---

def test_rejects_excessive_disagreement_yes_side():
    # Model says 52%, market says 32% — 20pt disagreement, over the 15% cap.
    ev = _ev(0.52, 0.32, "yes")
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("disagreement" in r.lower() for r in result.rejection_reasons)


def test_rejects_excessive_disagreement_no_side():
    # Model says 10%, market says 35% — NO-side edge 25%, over cap.
    ev = EVResult(p_model=0.10, implied_prob=0.35, edge=-0.25, no_edge=0.25,
                  raw_ev=-0.25, net_ev=-0.26, no_ev=0.24,
                  recommended_side="no", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("disagreement" in r.lower() for r in result.rejection_reasons)


def test_disagreement_exactly_at_cap_passes():
    # 15% disagreement exactly — boundary stays tradeable (strict > comparison).
    ev = _ev(0.65, 0.50, "yes")
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert not any("disagreement" in r.lower() for r in result.rejection_reasons)


def test_disagreement_cap_configurable():
    ev = _ev(0.52, 0.32, "yes")  # 20pt disagreement
    result = TradeFilter(max_disagreement=0.25).evaluate(
        ev_result=ev, confidence=0.8, daily_volume=5000,
        bid_ask_spread_cents=2, hours_to_expiry=48)
    assert not any("disagreement" in r.lower() for r in result.rejection_reasons)


# --- 2. YES longshot skip ---

def test_rejects_yes_longshot_under_30_cents():
    # YES buy at 20¢ with edge inside the cap — still rejected as longshot.
    ev = _ev(0.32, 0.20, "yes")
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("longshot" in r.lower() for r in result.rejection_reasons)


def test_no_side_at_low_yes_price_not_longshot_blocked():
    # NO recommendation on a 20¢ market is fading the longshot — allowed.
    ev = EVResult(p_model=0.10, implied_prob=0.20, edge=-0.10, no_edge=0.10,
                  raw_ev=-0.10, net_ev=-0.11, no_ev=0.09,
                  recommended_side="no", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert not any("longshot" in r.lower() for r in result.rejection_reasons)


def test_yes_above_30_cents_not_longshot_blocked():
    ev = _ev(0.55, 0.45, "yes")
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert not any("longshot" in r.lower() for r in result.rejection_reasons)
    assert result.qualifies is True


def test_yes_longshot_skip_can_be_disabled():
    ev = _ev(0.32, 0.20, "yes")
    result = TradeFilter(skip_yes_longshots=False).evaluate(
        ev_result=ev, confidence=0.8, daily_volume=5000,
        bid_ask_spread_cents=2, hours_to_expiry=48)
    assert not any("longshot" in r.lower() for r in result.rejection_reasons)


# --- 3. Conservative paper fills ---

def _decision(side="yes", price=50):
    return TradeDecision(
        approved=True, side=side, price_cents=price, quantity=3,
        position_size_dollars=1.5, rejection_reasons=[],
    )


def test_paper_fill_crosses_spread_yes(db_engine):
    # Conservative paper fill: YES buy pays the ask, not maker bid+1.
    engine = TradeEngine(db_engine)
    price = engine._compute_fill_price(_decision("yes", 50), yes_bid=48, yes_ask=52, is_paper=True)
    assert price == 52


def test_paper_fill_crosses_spread_no(db_engine):
    # NO buy costs 100 - yes_bid when crossing the spread.
    engine = TradeEngine(db_engine)
    price = engine._compute_fill_price(_decision("no", 50), yes_bid=48, yes_ask=52, is_paper=True)
    assert price == 100 - 48


def test_live_fill_price_unchanged_by_paper_flag(db_engine):
    # Taker now, not maker. With a per-series allow-list the default is maker
    # NOWHERE, so an unlisted series crosses the spread on the live path too:
    # YES pays the ask. That is the conservative direction and the approved
    # default — maker pricing needs the master flag AND the series listed.
    engine = TradeEngine(db_engine)
    price = engine._compute_fill_price(_decision("yes", 50), yes_bid=48, yes_ask=52, is_paper=False)
    assert price == 52  # the ask
