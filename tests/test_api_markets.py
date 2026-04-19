from datetime import datetime, timezone
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.markets import create_markets_router
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot


@pytest.fixture
def app_with_data(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates?",
            category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        session.add(Market(
            market_id="LAKERS-WIN", title="Will Lakers win?",
            category="Sports", close_date=datetime(2026, 4, 25, tzinfo=timezone.utc), status="open",
        ))
        session.add(PriceSnapshot(
            market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66, volume=1500,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
        ))
        session.add(PriceSnapshot(
            market_id="FED-RATE-JUL", yes_bid=66, yes_ask=68, last_price=67, volume=1600,
            timestamp=datetime(2026, 4, 19, 12, 30, tzinfo=timezone.utc),
        ))
        session.commit()
    app = FastAPI()
    app.include_router(create_markets_router(db_engine))
    return app


@pytest.fixture
def client(app_with_data):
    return TestClient(app_with_data)


def test_list_markets(client):
    response = client.get("/api/markets")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


def test_list_markets_filter_by_category(client):
    response = client.get("/api/markets?category=Sports")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["market_id"] == "LAKERS-WIN"


def test_get_market_detail(client):
    response = client.get("/api/markets/FED-RATE-JUL")
    assert response.status_code == 200
    data = response.json()
    assert data["market_id"] == "FED-RATE-JUL"
    assert data["title"] == "Will Fed raise rates?"


def test_get_market_not_found(client):
    response = client.get("/api/markets/NONEXISTENT")
    assert response.status_code == 404


def test_get_price_history(client):
    response = client.get("/api/markets/FED-RATE-JUL/prices")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["yes_bid"] == 65
