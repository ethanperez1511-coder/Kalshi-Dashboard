from datetime import datetime, timezone
from src.database import get_session, Base
from src.ingestion.price_recorder import record_price_snapshot
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_record_price_snapshot(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Test", category="finance",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        session.commit()

    record_price_snapshot(engine=db_engine, market_id="FED-RATE-JUL",
                         yes_bid=65, yes_ask=67, last_price=66, volume=1500)

    with get_session(db_engine) as session:
        snapshots = session.query(PriceSnapshot).all()
        assert len(snapshots) == 1
        assert snapshots[0].market_id == "FED-RATE-JUL"
        assert snapshots[0].yes_bid == 65
        assert snapshots[0].yes_ask == 67


def test_record_multiple_snapshots(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Test", category="finance",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        session.commit()

    record_price_snapshot(db_engine, "FED-RATE-JUL", 65, 67, 66, 1500)
    record_price_snapshot(db_engine, "FED-RATE-JUL", 66, 68, 67, 1600)

    with get_session(db_engine) as session:
        snapshots = session.query(PriceSnapshot).filter_by(market_id="FED-RATE-JUL").all()
        assert len(snapshots) == 2
