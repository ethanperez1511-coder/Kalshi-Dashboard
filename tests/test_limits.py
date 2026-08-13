from datetime import datetime, timezone
from src.database import get_session, Base
from src.risk.limits import LimitsChecker, LimitsResult
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings


def test_passes_when_within_limits(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)  # bankroll=100
    checker = LimitsChecker(db_engine)
    result = checker.check(
        trade_dollars=2.0,
        market_id="FED-RATE-JUL",
        market_category="Economics",
    )
    assert result.approved is True
    assert len(result.violations) == 0


def test_rejects_exceeding_single_trade(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=5.0, market_id="X", market_category="Economics")
    assert result.approved is False
    assert any("single trade" in v.lower() for v in result.violations)


def test_rejects_exceeding_total_exposure(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    # Create existing positions totaling $24 (close to $25 max = 25% of $100)
    with get_session(db_engine) as session:
        for i in range(12):
            session.add(Position(
                market_id=f"MKT-{i}", side="yes", entry_price=50,
                quantity=4, current_price=50, status="open",
            ))
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=2.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("total exposure" in v.lower() for v in result.violations)


def test_rejects_daily_loss_exceeded(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    # Create losing trades today
    with get_session(db_engine) as session:
        for i in range(3):
            session.add(Trade(
                market_id=f"LOSS-{i}", side="yes", action="buy", price=50,
                quantity=2, p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
                position_size_dollars=2.0, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-2.0,
                created_at=datetime.now(timezone.utc),
            ))
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=1.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("daily loss" in v.lower() for v in result.violations)


def test_rejects_drawdown_breaker(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.bankroll = 75.0  # Down from 100 peak
        settings.peak_bankroll = 100.0
        session.add(settings)
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=1.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("drawdown" in v.lower() for v in result.violations)


# --------------------------------------------------------------------------
# Correlated clusters: series + date, not date alone
# --------------------------------------------------------------------------

class TestClusterKey:
    """`_extract_cluster_key` returned `market_id.split("-")[1]`, which for
    every non-MVE ticker is a DATE. Seven independent cities shared one cluster
    and one 10%-of-bankroll cap, while the genuinely correlated thing — the
    ladder of strikes on one city-day — was grouped only incidentally through
    that same date.
    """

    def test_one_city_day_ladder_is_one_cluster(self):
        from src.risk.limits import _extract_cluster_key as key

        ladder = [f"KXHIGHNY-26AUG13-T{t}" for t in (76, 83, 85, 90, 92, 95)]
        assert len({key(t) for t in ladder}) == 1, (
            "every strike on a city-day reads off one outcome"
        )

    def test_two_cities_on_the_same_day_are_not_one_cluster(self):
        from src.risk.limits import _extract_cluster_key as key

        assert key("KXHIGHNY-26AUG13-T92") != key("KXHIGHMIA-26AUG13-T88"), (
            "New York and Miami weather are not correlated; sharing a cap "
            "capped the whole weather book at about three positions"
        )

    def test_one_city_on_two_days_is_not_one_cluster(self):
        from src.risk.limits import _extract_cluster_key as key

        assert key("KXHIGHNY-26AUG13-T92") != key("KXHIGHNY-26AUG14-T92")

    def test_mve_parlay_legs_still_share_their_event(self):
        """The case the function was written for, unchanged."""
        from src.risk.limits import _extract_cluster_key as key

        base = "KXMVESPORTSMULTIGAMEEXTENDED-S2026XXXX"
        assert key(f"{base}-YYYY") == key(f"{base}-ZZZZ") == "S2026XXXX"

    def test_a_ticker_with_no_segments_is_its_own_cluster(self):
        from src.risk.limits import _extract_cluster_key as key

        assert key("SINGLE") == "SINGLE"

    def test_the_hard_limits_are_untouched(self):
        """The cluster cap is the only number this change moves. Quarter-Kelly,
        3% per trade, 25% total exposure and the 20% drawdown breaker are
        enforced ceilings, and a clustering fix must not have loosened one."""
        from src.models.settings import TradingSettings
        from src.trading_config import MAX_CLUSTER_EXPOSURE

        assert MAX_CLUSTER_EXPOSURE == 0.10
        # Read the column defaults, not an unsaved instance: SQLAlchemy applies
        # them at INSERT, so `TradingSettings().kelly_fraction` is None and an
        # assertion against it would pass for the wrong reason.
        columns = TradingSettings.__table__.c
        limits = {name: columns[name].default.arg for name in (
            "kelly_fraction", "max_single_trade_pct",
            "max_total_exposure_pct", "drawdown_circuit_breaker_pct",
        )}
        assert limits == {
            "kelly_fraction": 0.25,
            "max_single_trade_pct": 0.03,
            "max_total_exposure_pct": 0.25,
            "drawdown_circuit_breaker_pct": 0.20,
        }, limits
