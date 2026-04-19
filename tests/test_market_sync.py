from datetime import datetime, timezone
import pytest
from src.database import get_session, Base
from src.ingestion.market_sync import sync_markets
from src.kalshi.schemas import KalshiMarket
from src.models.market import Market


@pytest.fixture
def sample_kalshi_markets():
    return [
        KalshiMarket(
            ticker="FED-RATE-JUL",
            title="Will Fed raise rates in July?",
            category="Economics",
            close_time=datetime(2026, 7, 15, tzinfo=timezone.utc),
            status="open",
            rules_primary="Resolves Yes if Fed raises rates.",
            yes_bid=65, yes_ask=67, last_price=66, volume=50000, volume_24h=1500,
        ),
        KalshiMarket(
            ticker="LAKERS-WIN-042026",
            title="Will Lakers win tonight?",
            category="Sports",
            close_time=datetime(2026, 4, 20, tzinfo=timezone.utc),
            status="open",
            rules_primary="Resolves Yes if Lakers win.",
            yes_bid=42, yes_ask=44, last_price=43, volume=30000, volume_24h=800,
        ),
    ]


def test_sync_markets_inserts_new(db_engine, sample_kalshi_markets):
    Base.metadata.create_all(db_engine)
    sync_markets(db_engine, sample_kalshi_markets)
    with get_session(db_engine) as session:
        markets = session.query(Market).all()
        assert len(markets) == 2
        fed = session.query(Market).filter_by(market_id="FED-RATE-JUL").first()
        assert fed.title == "Will Fed raise rates in July?"
        assert fed.category == "Economics"


def test_sync_markets_updates_existing(db_engine, sample_kalshi_markets):
    Base.metadata.create_all(db_engine)
    sync_markets(db_engine, sample_kalshi_markets)
    sample_kalshi_markets[0].title = "Updated title"
    sample_kalshi_markets[0].status = "closed"
    sync_markets(db_engine, sample_kalshi_markets)
    with get_session(db_engine) as session:
        markets = session.query(Market).all()
        assert len(markets) == 2
        fed = session.query(Market).filter_by(market_id="FED-RATE-JUL").first()
        assert fed.title == "Updated title"
        assert fed.status == "closed"
