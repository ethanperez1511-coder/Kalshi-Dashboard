from __future__ import annotations

import pytest
from sqlalchemy import Engine

from src.database import get_engine, get_session, Base
from src.models.trade import Trade


@pytest.fixture
def db_engine(tmp_path) -> Engine:
    db_path = tmp_path / "test_trade.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def _make_trade(**kwargs) -> Trade:
    defaults = dict(
        market_id="MKT-001",
        side="yes",
        action="buy",
        price=40,
        quantity=10,
        p_model=0.65,
        implied_prob=0.40,
        edge=0.25,
        net_ev=0.18,
        position_size_dollars=50.0,
        confidence=0.75,
        reasoning="Strong edge detected by model.",
        is_paper=True,
        status="open",
    )
    defaults.update(kwargs)
    return Trade(**defaults)


def test_create_trade(db_engine: Engine) -> None:
    with get_session(db_engine) as session:
        trade = _make_trade()
        session.add(trade)
        session.flush()
        trade_id = trade.id

    with get_session(db_engine) as session:
        fetched = session.get(Trade, trade_id)
        assert fetched is not None
        assert fetched.market_id == "MKT-001"
        assert fetched.side == "yes"
        assert fetched.action == "buy"
        assert fetched.price == 40
        assert fetched.quantity == 10
        assert fetched.p_model == pytest.approx(0.65)
        assert fetched.implied_prob == pytest.approx(0.40)
        assert fetched.edge == pytest.approx(0.25)
        assert fetched.net_ev == pytest.approx(0.18)
        assert fetched.position_size_dollars == pytest.approx(50.0)
        assert fetched.confidence == pytest.approx(0.75)
        assert fetched.reasoning == "Strong edge detected by model."
        assert fetched.is_paper is True
        assert fetched.status == "open"
        assert fetched.exit_price is None
        assert fetched.realized_pnl is None
        assert fetched.created_at is not None


def test_trade_pnl_on_close(db_engine: Engine) -> None:
    with get_session(db_engine) as session:
        trade = _make_trade(price=40, quantity=10)
        session.add(trade)
        session.flush()
        trade_id = trade.id

    # Close the trade: exit at 70, pnl = (70 - 40) * 10 / 100 = 3.0
    with get_session(db_engine) as session:
        trade = session.get(Trade, trade_id)
        trade.exit_price = 70
        trade.realized_pnl = (trade.exit_price - trade.price) * trade.quantity / 100
        trade.status = "closed"

    with get_session(db_engine) as session:
        fetched = session.get(Trade, trade_id)
        assert fetched.exit_price == 70
        assert fetched.realized_pnl == pytest.approx(3.0)
        assert fetched.status == "closed"
