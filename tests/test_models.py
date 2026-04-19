from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.orderbook import OrderbookSnapshot


def test_create_market(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        market = Market(
            market_id="kalshi-market-123",
            title="Will Fed raise rates in July?",
            category="finance",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
            status="open",
            rules="Resolves Yes if Fed raises rates at July FOMC meeting.",
        )
        session.add(market)
        session.commit()

        fetched = session.query(Market).filter_by(market_id="kalshi-market-123").first()
        assert fetched is not None
        assert fetched.title == "Will Fed raise rates in July?"
        assert fetched.category == "finance"
        assert fetched.status == "open"


def test_create_price_snapshot(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        market = Market(
            market_id="kalshi-market-123",
            title="Test Market",
            category="finance",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
            status="open",
        )
        session.add(market)
        session.commit()

        snapshot = PriceSnapshot(
            market_id="kalshi-market-123",
            yes_bid=65,
            yes_ask=67,
            last_price=66,
            volume=1500,
            timestamp=datetime.now(timezone.utc),
        )
        session.add(snapshot)
        session.commit()

        fetched = session.query(PriceSnapshot).filter_by(market_id="kalshi-market-123").first()
        assert fetched is not None
        assert fetched.yes_bid == 65
        assert fetched.yes_ask == 67
        assert fetched.volume == 1500


def test_create_orderbook_snapshot(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        market = Market(
            market_id="kalshi-market-123",
            title="Test Market",
            category="finance",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
            status="open",
        )
        session.add(market)
        session.commit()

        ob = OrderbookSnapshot(
            market_id="kalshi-market-123",
            yes_bids='[["65", "100"], ["64", "200"]]',
            yes_asks='[["67", "150"], ["68", "300"]]',
            timestamp=datetime.now(timezone.utc),
        )
        session.add(ob)
        session.commit()

        fetched = session.query(OrderbookSnapshot).filter_by(market_id="kalshi-market-123").first()
        assert fetched is not None
        assert '"65"' in fetched.yes_bids
