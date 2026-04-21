from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.equity import build_equity_curve


def _seed_trades(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        trades = [
            Trade(
                market_id="T1", side="yes", action="buy", price=50, quantity=2,
                p_model=0.7, implied_prob=0.5, edge=0.2, net_ev=0.19,
                position_size_dollars=1.0, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.0,
                created_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="T2", side="yes", action="buy", price=60, quantity=2,
                p_model=0.8, implied_prob=0.6, edge=0.2, net_ev=0.19,
                position_size_dollars=1.2, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-1.20,
                created_at=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="T3", side="yes", action="buy", price=40, quantity=2,
                p_model=0.6, implied_prob=0.4, edge=0.2, net_ev=0.19,
                position_size_dollars=0.8, confidence=0.7, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.20,
                created_at=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()


def test_equity_curve_length(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    assert len(curve) == 4


def test_equity_curve_starts_at_initial_bankroll(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    assert curve[0]["bankroll"] == 100.0


def test_equity_curve_cumulative(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    assert abs(curve[1]["bankroll"] - 101.0) < 0.01
    assert abs(curve[2]["bankroll"] - 99.80) < 0.01
    assert abs(curve[3]["bankroll"] - 101.0) < 0.01


def test_equity_curve_tracks_peak(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    assert curve[1]["peak"] == 101.0
    assert curve[2]["peak"] == 101.0
    assert curve[3]["peak"] == 101.0


def test_equity_curve_empty(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    curve = build_equity_curve(db_engine)
    assert len(curve) == 1
    assert curve[0]["bankroll"] == 100.0
