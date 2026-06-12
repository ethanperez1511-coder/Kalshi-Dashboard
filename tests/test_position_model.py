from __future__ import annotations

import pytest
from sqlalchemy import Engine

from src.database import get_engine, get_session, Base
from src.models.position import Position


@pytest.fixture
def db_engine(tmp_path) -> Engine:
    db_path = tmp_path / "test_position.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def test_create_position(db_engine: Engine) -> None:
    with get_session(db_engine) as session:
        pos = Position(
            market_id="MKT-001",
            side="yes",
            entry_price=40,
            quantity=10,
            current_price=40,
            status="open",
        )
        session.add(pos)
        session.flush()
        pos_id = pos.id

    with get_session(db_engine) as session:
        fetched = session.get(Position, pos_id)
        assert fetched is not None
        assert fetched.market_id == "MKT-001"
        assert fetched.side == "yes"
        assert fetched.entry_price == 40
        assert fetched.quantity == 10
        assert fetched.status == "open"
        assert fetched.closed_at is None


def test_position_unrealized_pnl(db_engine: Engine) -> None:
    # YES side: profit when current > entry
    yes_pos = Position(
        market_id="MKT-001",
        side="yes",
        entry_price=40,
        quantity=10,
        current_price=60,
        status="open",
    )
    # (60 - 40) * 10 / 100 = 2.0
    assert yes_pos.unrealized_pnl == pytest.approx(2.0)

    # YES side: loss when current < entry
    yes_loss = Position(
        market_id="MKT-001",
        side="yes",
        entry_price=60,
        quantity=10,
        current_price=40,
        status="open",
    )
    # (40 - 60) * 10 / 100 = -2.0
    assert yes_loss.unrealized_pnl == pytest.approx(-2.0)

    # NO side: prices stored in NO-cost terms, so profit when current > entry
    no_pos = Position(
        market_id="MKT-002",
        side="no",
        entry_price=40,
        quantity=10,
        current_price=60,
        status="open",
    )
    # (60 - 40) * 10 / 100 = 2.0
    assert no_pos.unrealized_pnl == pytest.approx(2.0)

    # NO side: loss when current < entry
    no_loss = Position(
        market_id="MKT-002",
        side="no",
        entry_price=60,
        quantity=10,
        current_price=40,
        status="open",
    )
    # (40 - 60) * 10 / 100 = -2.0
    assert no_loss.unrealized_pnl == pytest.approx(-2.0)


def test_position_cost_basis(db_engine: Engine) -> None:
    # YES side: entry * qty / 100
    yes_pos = Position(
        market_id="MKT-001",
        side="yes",
        entry_price=40,
        quantity=10,
        current_price=40,
        status="open",
    )
    # 40 * 10 / 100 = 4.0
    assert yes_pos.cost_basis == pytest.approx(4.0)

    # NO side: entry is already the NO cost — same formula as YES
    no_pos = Position(
        market_id="MKT-002",
        side="no",
        entry_price=40,
        quantity=10,
        current_price=40,
        status="open",
    )
    # 40 * 10 / 100 = 4.0
    assert no_pos.cost_basis == pytest.approx(4.0)
