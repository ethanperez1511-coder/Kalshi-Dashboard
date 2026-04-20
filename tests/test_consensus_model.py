from datetime import datetime, timezone

from src.database import get_session, Base
from src.modeling.models.consensus import ConsensusModel
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_consensus_model_estimates_from_spread(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="TEST-MKT", title="Test Market", category="General",
                          close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        for i in range(10):
            session.add(PriceSnapshot(market_id="TEST-MKT", yes_bid=65, yes_ask=67, last_price=66,
                                     volume=2000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc)))
        session.commit()
    model = ConsensusModel()
    result = model.estimate("TEST-MKT", "Test Market", current_price=66, engine=db_engine)
    assert result is not None
    assert 0.60 <= result.p_model <= 0.72
    assert result.confidence > 0


def test_consensus_model_returns_none_with_no_data(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="EMPTY-MKT", title="No Data", category="General",
                          close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        session.commit()
    model = ConsensusModel()
    result = model.estimate("EMPTY-MKT", "No Data", current_price=50, engine=db_engine)
    assert result is None


def test_consensus_model_detects_price_trend(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="TREND-MKT", title="Trending Market", category="General",
                          close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        for i, price in enumerate([50, 52, 55, 58, 60, 62, 65, 67, 69, 70]):
            session.add(PriceSnapshot(market_id="TREND-MKT", yes_bid=price, yes_ask=price + 2,
                                     last_price=price + 1, volume=1000,
                                     timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc)))
        session.commit()
    model = ConsensusModel()
    result = model.estimate("TREND-MKT", "Trending Market", current_price=70, engine=db_engine)
    assert result is not None
    assert result.p_model >= 0.70


def test_consensus_handles_any_category():
    model = ConsensusModel()
    assert model.category == "fallback"
