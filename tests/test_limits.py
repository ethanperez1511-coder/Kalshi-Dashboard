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
