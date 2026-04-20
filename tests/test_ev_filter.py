from __future__ import annotations

from src.ev.calculator import EVResult
from src.ev.filter import TradeFilter


def test_qualifies_good_trade():
    ev = EVResult(p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
                  raw_ev=0.12, net_ev=0.10, no_ev=-0.14, recommended_side="yes", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is True


def test_rejects_low_edge():
    ev = EVResult(p_model=0.67, implied_prob=0.65, edge=0.02, no_edge=-0.02,
                  raw_ev=0.02, net_ev=0.01, no_ev=-0.03, recommended_side="yes", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("edge" in r.lower() for r in result.rejection_reasons)


def test_rejects_negative_ev():
    ev = EVResult(p_model=0.50, implied_prob=0.65, edge=-0.15, no_edge=0.15,
                  raw_ev=-0.15, net_ev=-0.16, no_ev=0.14, recommended_side="no", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    # No side has 0.15 edge and 0.14 EV — should qualify
    assert result.qualifies is True


def test_rejects_low_volume():
    ev = EVResult(p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
                  raw_ev=0.12, net_ev=0.10, no_ev=-0.14, recommended_side="yes", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=100,
                                    bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False


def test_rejects_wide_spread():
    ev = EVResult(p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
                  raw_ev=0.12, net_ev=0.10, no_ev=-0.14, recommended_side="yes", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=8, hours_to_expiry=48)
    assert result.qualifies is False


def test_rejects_expiring_soon():
    ev = EVResult(p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
                  raw_ev=0.12, net_ev=0.10, no_ev=-0.14, recommended_side="yes", fee_rate=0.01)
    result = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                    bid_ask_spread_cents=2, hours_to_expiry=0.5)
    assert result.qualifies is False


def test_medium_confidence_raises_threshold():
    ev = EVResult(p_model=0.71, implied_prob=0.65, edge=0.06, no_edge=-0.06,
                  raw_ev=0.06, net_ev=0.04, no_ev=-0.08, recommended_side="yes", fee_rate=0.01)
    result_high = TradeFilter().evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                                         bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result_high.qualifies is True
    result_med = TradeFilter().evaluate(ev_result=ev, confidence=0.5, daily_volume=5000,
                                        bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result_med.qualifies is False
