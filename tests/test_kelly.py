from src.risk.kelly import kelly_size, KellyResult


def test_kelly_basic():
    result = kelly_size(
        p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    assert result.full_kelly > 0
    assert result.fractional_kelly > 0
    assert result.fractional_kelly < result.full_kelly
    assert result.recommended_dollars > 0
    assert result.recommended_quantity >= 1


def test_kelly_negative_edge_returns_zero():
    result = kelly_size(
        p_model=0.50, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    assert result.full_kelly == 0
    assert result.recommended_dollars == 0
    assert result.recommended_quantity == 0


def test_kelly_respects_fraction():
    full = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=1.0)
    quarter = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25)
    assert abs(quarter.recommended_dollars - full.recommended_dollars * 0.25) < 0.5


def test_kelly_small_bankroll():
    result = kelly_size(
        p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    # With $100, position should be small
    assert result.recommended_dollars <= 10.0


def test_kelly_scales_with_bankroll():
    small = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25)
    large = kelly_size(p_model=0.77, price_cents=65, bankroll=1000.0, kelly_fraction=0.25)
    assert large.recommended_dollars > small.recommended_dollars
