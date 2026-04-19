from src.database import get_session, Base
from src.demo.seed import seed_demo_data
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_seed_creates_markets(db_engine):
    Base.metadata.create_all(db_engine)
    seed_demo_data(db_engine)
    with get_session(db_engine) as session:
        markets = session.query(Market).all()
        assert len(markets) >= 5


def test_seed_creates_price_history(db_engine):
    Base.metadata.create_all(db_engine)
    seed_demo_data(db_engine)
    with get_session(db_engine) as session:
        snapshots = session.query(PriceSnapshot).all()
        assert len(snapshots) >= 10


def test_seed_is_idempotent(db_engine):
    Base.metadata.create_all(db_engine)
    seed_demo_data(db_engine)
    seed_demo_data(db_engine)
    with get_session(db_engine) as session:
        markets = session.query(Market).all()
        market_ids = [m.market_id for m in markets]
        assert len(market_ids) == len(set(market_ids))
