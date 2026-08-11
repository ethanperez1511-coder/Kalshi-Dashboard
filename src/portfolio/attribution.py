"""Which model is the paper record actually made of?

The 50-trade gate counts trades, not evidence. With Polymarket at zero after
the Phase 1.5 horizon ruling and the price-derived models gated off, every
paper trade currently comes from one model — so the counter can reach 50/50 and
report "validated" about a system validated in exactly one corner, with zero
settled evidence for anything else.

These counts make that visible. Enforcing a per-model minimum before live is a
policy decision, not something to slip in behind a helper function.
"""
from __future__ import annotations

from typing import Dict

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.trade import Trade

UNATTRIBUTED = "unattributed"


def trades_by_model(engine: Engine, paper_only: bool = True) -> Dict[str, int]:
    """Placed trades per model. Rows predating attribution count as unattributed."""
    with get_session(engine) as session:
        stmt = select(Trade.model_name, func.count()).group_by(Trade.model_name)
        if paper_only:
            stmt = stmt.where(Trade.is_paper.is_(True))
        rows = session.execute(stmt).all()
    return {(name or UNATTRIBUTED): count for name, count in rows}


def settled_by_model(engine: Engine, paper_only: bool = True) -> Dict[str, int]:
    """Trades per model that have actually SETTLED.

    The distinction matters: a placed trade is a decision, a settled trade is
    the only thing that carries information about whether the decision was any
    good. Calibration can only be computed from the latter.
    """
    with get_session(engine) as session:
        stmt = (
            select(Trade.model_name, func.count())
            .where(Trade.status == "closed")
            .group_by(Trade.model_name)
        )
        if paper_only:
            stmt = stmt.where(Trade.is_paper.is_(True))
        rows = session.execute(stmt).all()
    return {(name or UNATTRIBUTED): count for name, count in rows}


def models_without_settled_evidence(engine: Engine, known_models) -> list:
    """Models that have never produced a settled paper trade.

    A non-empty result means the paper record cannot speak for those models,
    however high the overall count has climbed.
    """
    settled = settled_by_model(engine)
    return sorted(m for m in known_models if settled.get(m, 0) == 0)
