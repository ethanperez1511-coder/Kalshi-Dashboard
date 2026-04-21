from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.metrics import compute_metrics


def _seed_closed_trades(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        trades = [
            Trade(
                market_id="W1", side="yes", action="buy", price=50, quantity=2,
                p_model=0.70, implied_prob=0.50, edge=0.20, net_ev=0.19,
                position_size_dollars=1.0, confidence=0.85, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.0,
                created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="W2", side="yes", action="buy", price=60, quantity=2,
                p_model=0.80, implied_prob=0.60, edge=0.20, net_ev=0.19,
                position_size_dollars=1.20, confidence=0.80, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=0.80,
                created_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="L1", side="yes", action="buy", price=55, quantity=2,
                p_model=0.65, implied_prob=0.55, edge=0.10, net_ev=0.09,
                position_size_dollars=1.10, confidence=0.75, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-1.10,
                created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()


def test_metrics_win_rate(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert abs(m["win_rate"] - 66.67) < 0.1


def test_metrics_total_pnl(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert abs(m["total_pnl"] - 0.70) < 0.01


def test_metrics_total_return_pct(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert abs(m["total_return_pct"] - 0.70) < 0.01


def test_metrics_average_edge(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert abs(m["avg_edge"] - 0.1667) < 0.01


def test_metrics_trade_count(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert m["total_trades"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1


def test_metrics_calibration(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert "calibration_error" in m
    assert m["calibration_error"] >= 0


def test_metrics_empty(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    m = compute_metrics(db_engine)
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0
    assert m["total_pnl"] == 0
