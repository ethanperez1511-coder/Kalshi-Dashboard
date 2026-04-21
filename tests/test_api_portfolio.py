from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.database import get_session, Base
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.api.portfolio import create_portfolio_router
from fastapi import FastAPI


def _create_test_app(db_engine):
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(create_portfolio_router(db_engine))
    return TestClient(app)


def _seed_data(db_engine):
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Position(
            market_id="MKT-1", side="yes", entry_price=60,
            quantity=3, current_price=65, status="open",
        ))
        session.add(Trade(
            market_id="MKT-1", side="yes", action="buy", price=60,
            quantity=3, p_model=0.75, implied_prob=0.60, edge=0.15,
            net_ev=0.14, position_size_dollars=1.80, confidence=0.85,
            reasoning="test trade", is_paper=True, status="filled",
            created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        ))
        session.add(Trade(
            market_id="MKT-0", side="yes", action="buy", price=50,
            quantity=2, p_model=0.70, implied_prob=0.50, edge=0.20,
            net_ev=0.19, position_size_dollars=1.0, confidence=0.80,
            reasoning="closed trade", is_paper=True, status="closed",
            exit_price=100, realized_pnl=1.0,
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        ))
        session.commit()


def test_get_summary(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "bankroll" in data
    assert "open_position_count" in data
    assert data["open_position_count"] == 1


def test_get_positions(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["market_id"] == "MKT-1"


def test_get_trades(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/trades")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_get_trades_filter_status(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/trades?status=closed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["market_id"] == "MKT-0"


def test_get_metrics(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 1
    assert data["wins"] == 1


def test_get_equity_curve(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/equity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["bankroll"] == 100.0
