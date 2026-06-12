from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.tracker import PortfolioTracker


def _setup(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Position(
            market_id="MKT-1", side="yes", entry_price=60,
            quantity=3, current_price=60, status="open",
        ))
        session.add(Trade(
            market_id="MKT-1", side="yes", action="buy", price=60,
            quantity=3, p_model=0.75, implied_prob=0.60, edge=0.15,
            net_ev=0.14, position_size_dollars=1.80, confidence=0.85,
            reasoning="test", is_paper=True, status="filled",
        ))
        session.commit()


def test_close_position_win(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="MKT-1", exit_price=100)
    assert result["realized_pnl"] > 0
    assert result["status"] == "closed"

    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="MKT-1").first()
        assert pos.status == "closed"
        assert pos.closed_at is not None

    with get_session(db_engine) as session:
        trade = session.query(Trade).filter_by(market_id="MKT-1").first()
        assert trade.status == "closed"
        assert trade.exit_price == 100
        assert trade.realized_pnl > 0


def test_close_position_loss(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="MKT-1", exit_price=0)
    assert result["realized_pnl"] < 0

    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="MKT-1").first()
        assert pos.status == "closed"


def test_close_updates_bankroll(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    tracker.close_position(market_id="MKT-1", exit_price=100)
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.bankroll == 101.14  # 101.20 gross - 0.06 fee


def test_close_updates_peak_bankroll(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    tracker.close_position(market_id="MKT-1", exit_price=100)
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.peak_bankroll == 101.14  # 101.20 gross - 0.06 fee


def test_close_nonexistent_returns_none(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="NOPE", exit_price=100)
    assert result is None


def test_get_open_positions(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    positions = tracker.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["market_id"] == "MKT-1"
    assert positions[0]["unrealized_pnl"] == 0.0


def test_get_portfolio_summary(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    summary = tracker.get_summary()
    assert summary["bankroll"] == 100.0
    assert summary["open_position_count"] == 1
    assert summary["total_exposure"] > 0
    assert "total_return_pct" in summary
    assert "max_drawdown_pct" in summary
