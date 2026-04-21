from datetime import datetime, timezone
from src.database import get_session, Base
from src.trading.engine import TradeEngine
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.models.position import Position
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.risk.manager import TradeDecision


def _setup_market(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates?",
            category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()


def test_execute_paper_trade(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=True, side="yes", position_size_dollars=1.30,
        quantity=2, price_cents=65, rejection_reasons=[],
    )
    trade = engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="Finance model",
    )
    assert trade is not None
    assert trade["is_paper"] is True
    assert trade["status"] == "filled"
    assert trade["side"] == "yes"

    # Verify position was created
    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="FED-RATE-JUL").first()
        assert pos is not None
        assert pos.side == "yes"
        assert pos.entry_price == 65
        assert pos.quantity == 2
        assert pos.status == "open"


def test_execute_rejected_trade_returns_none(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=False, side="yes", position_size_dollars=0,
        quantity=0, price_cents=65, rejection_reasons=["Too risky"],
    )
    trade = engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="test",
    )
    assert trade is None


def test_paper_trade_count_increments(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=True, side="yes", position_size_dollars=1.30,
        quantity=2, price_cents=65, rejection_reasons=[],
    )
    engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="test",
    )
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.paper_trade_count == 1


def test_live_mode_blocked_without_enough_paper_trades(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.mode = "live"
        settings.paper_trade_count = 10  # Under 50 threshold
        session.add(settings)
        session.commit()
    engine = TradeEngine(db_engine)
    assert engine.can_trade_live() is False
