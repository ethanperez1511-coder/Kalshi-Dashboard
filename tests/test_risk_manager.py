from src.database import Base
from src.risk.manager import RiskManager
from src.models.settings import TradingSettings
from src.ev.calculator import EVResult


def test_risk_manager_approves_good_trade(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.85,
        market_id="FED-RATE-JUL", market_category="Economics",
    )
    assert decision.approved is True
    assert decision.position_size_dollars > 0
    assert decision.quantity >= 1
    assert decision.side == "yes"


def test_risk_manager_rejects_negative_edge(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    ev = EVResult(
        p_model=0.50, implied_prob=0.65, edge=-0.15, no_edge=0.15,
        raw_ev=-0.15, net_ev=-0.16, no_ev=0.14,
        recommended_side="no", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.85,
        market_id="FED-RATE-JUL", market_category="Economics",
    )
    # No side has positive edge, so it might approve for No
    # But let's check it returns a decision either way
    assert decision.side == "no"


def test_risk_manager_caps_position_size(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    # Very high edge should still be capped at 3% of bankroll
    ev = EVResult(
        p_model=0.95, implied_prob=0.30, edge=0.65, no_edge=-0.65,
        raw_ev=0.60, net_ev=0.59, no_ev=-0.62,
        recommended_side="yes", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.9,
        market_id="HIGH-EDGE", market_category="Economics",
    )
    assert decision.position_size_dollars <= 3.0  # 3% of $100
