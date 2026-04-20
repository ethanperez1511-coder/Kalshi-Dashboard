from __future__ import annotations

from datetime import datetime, timezone

from src.database import get_session, Base
from src.ev.scorer import score_all_markets
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.opportunity import Opportunity


def test_score_all_markets_creates_opportunities(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="FED-RATE-JUL", title="Will Fed raise rates in July?",
                          category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        for i in range(10):
            session.add(PriceSnapshot(market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                                     volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc)))
        session.commit()
    results = score_all_markets(db_engine)
    assert len(results) >= 1
    with get_session(db_engine) as session:
        opps = session.query(Opportunity).all()
        assert len(opps) >= 1


def test_score_updates_existing_opportunities(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="FED-RATE-JUL", title="Will Fed raise rates in July?",
                          category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        for i in range(10):
            session.add(PriceSnapshot(market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                                     volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc)))
        session.commit()
    score_all_markets(db_engine)
    score_all_markets(db_engine)
    with get_session(db_engine) as session:
        opps = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").all()
        assert len(opps) == 1


def test_score_skips_closed_markets(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="CLOSED-MKT", title="Closed market",
                          category="Economics", close_date=datetime(2026, 1, 1, tzinfo=timezone.utc), status="closed"))
        for i in range(10):
            session.add(PriceSnapshot(market_id="CLOSED-MKT", yes_bid=65, yes_ask=67, last_price=66,
                                     volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc)))
        session.commit()
    score_all_markets(db_engine)
    with get_session(db_engine) as session:
        opp = session.query(Opportunity).filter_by(market_id="CLOSED-MKT").first()
        assert opp is None
