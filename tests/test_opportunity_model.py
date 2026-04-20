from __future__ import annotations

from datetime import datetime, timezone

from src.database import get_session, Base
from src.models.opportunity import Opportunity
from src.models.market import Market


def test_create_opportunity(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="FED-RATE-JUL", title="Will Fed raise rates?",
                          category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        session.commit()
        opp = Opportunity(market_id="FED-RATE-JUL", p_model=0.77, implied_prob=0.65, edge=0.12,
                         net_ev=0.10, recommended_side="yes", confidence=0.85, status="qualifying",
                         reasoning="Finance model", model_name="FinanceModel")
        session.add(opp)
        session.commit()
        fetched = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").first()
        assert fetched is not None
        assert fetched.p_model == 0.77
        assert fetched.edge == 0.12
        assert fetched.recommended_side == "yes"


def test_opportunity_updates_on_rescore(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(market_id="FED-RATE-JUL", title="Will Fed raise rates?",
                          category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open"))
        opp = Opportunity(market_id="FED-RATE-JUL", p_model=0.77, implied_prob=0.65, edge=0.12,
                         net_ev=0.10, recommended_side="yes", confidence=0.85, status="qualifying",
                         reasoning="Initial", model_name="FinanceModel")
        session.add(opp)
        session.commit()
        existing = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").first()
        existing.p_model = 0.72
        existing.edge = 0.07
        session.commit()
        all_opps = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").all()
        assert len(all_opps) == 1
        assert all_opps[0].p_model == 0.72
