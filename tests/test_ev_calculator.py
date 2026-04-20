from __future__ import annotations

from src.ev.calculator import calculate_ev


def test_positive_ev():
    result = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.01)
    assert result.raw_ev > 0
    assert result.net_ev > 0
    assert result.edge > 0
    assert result.edge == 0.77 - 0.65


def test_negative_ev():
    result = calculate_ev(p_model=0.50, price_cents=65, fee_rate=0.01)
    assert result.raw_ev < 0
    assert result.net_ev < 0


def test_ev_formula():
    result = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.0)
    price = 0.65
    expected_ev = 0.77 * (1 - price) - (1 - 0.77) * price
    assert abs(result.raw_ev - expected_ev) < 0.001


def test_fees_reduce_ev():
    no_fee = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.0)
    with_fee = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.05)
    assert with_fee.net_ev < no_fee.net_ev


def test_ev_for_no_position():
    result = calculate_ev(p_model=0.30, price_cents=45, fee_rate=0.01)
    assert result.no_edge == (1 - 0.30) - (1 - 0.45)
    assert result.no_ev > 0
    assert result.recommended_side == "no"


def test_recommended_side_yes():
    result = calculate_ev(p_model=0.80, price_cents=65, fee_rate=0.01)
    assert result.recommended_side == "yes"
