import random
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from fastapi import FastAPI
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.api.backtest import create_backtest_router


def _create_test_app(db_engine):
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(create_backtest_router(db_engine))
    return TestClient(app)


def _seed_markets(db_engine):
    with get_session(db_engine) as session:
        random.seed(42)
        session.add(Market(
            market_id="BT-1", title="Test market",
            category="Sports", close_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            status="closed",
        ))
        base = 45
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            session.add(PriceSnapshot(
                market_id="BT-1", yes_bid=price, yes_ask=price + 2,
                last_price=price + 1, volume=2000, timestamp=ts,
            ))
            base = price
        session.commit()


def test_run_backtest(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    resp = client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
        "initial_bankroll": 100.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "completed"


def test_get_backtest_report(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    resp = client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
    })
    run_id = resp.json()["run_id"]

    resp = client.get(f"/api/backtest/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert "equity_curve" in data
    assert "limitations" in data


def test_get_backtest_not_found(db_engine):
    client = _create_test_app(db_engine)
    resp = client.get("/api/backtest/999")
    assert resp.status_code == 404


def test_list_backtests(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
    })
    resp = client.get("/api/backtest")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
