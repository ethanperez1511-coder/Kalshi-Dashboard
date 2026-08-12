"""Read/write helpers for the Kalshi↔Polymarket match decisions.

A decision, once made, is permanent until a human changes it — that is what
stops the fail-closed policy from re-asking the same question every five
minutes and what makes a single approval worth making.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Engine

from src.database import get_session
from src.models.match_map import MarketMatchMap

logger = logging.getLogger(__name__)


def get_decision(engine: Engine, kalshi_market_id: str) -> Optional[Tuple[str, str]]:
    """Return (status, poly_condition_id) for a market, or None if unseen.

    Without an engine there is nowhere to remember decisions, so every pair
    looks unseen. The entity comparison is pure and still runs — persistence is
    what degrades, never the safety check.
    """
    if engine is None:
        return None
    with get_session(engine) as session:
        row = (
            session.query(MarketMatchMap)
            .filter_by(kalshi_market_id=kalshi_market_id)
            .first()
        )
        return (row.status, row.poly_condition_id) if row else None


def all_decisions(engine: Engine) -> Dict[str, Tuple[str, str]]:
    """Every recorded decision in one query.

    get_decision() per market is one network round-trip each; against Neon that
    is the difference between a scoring stage that finishes and one that does
    not.
    """
    if engine is None:
        return {}
    with get_session(engine) as session:
        return {
            row.kalshi_market_id: (row.status, row.poly_condition_id)
            for row in session.query(MarketMatchMap).all()
        }


def _upsert(engine: Engine, kalshi_market_id: str, **fields) -> None:
    if engine is None:
        return
    with get_session(engine) as session:
        row = (
            session.query(MarketMatchMap)
            .filter_by(kalshi_market_id=kalshi_market_id)
            .first()
        )
        if row is None:
            row = MarketMatchMap(kalshi_market_id=kalshi_market_id)
            session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def record_pending(
    engine: Engine,
    kalshi_market_id: str,
    poly_condition_id: str,
    similarity: float,
    kalshi_title: str,
    poly_question: str,
    verdict: str,
    reason: str,
) -> None:
    """Queue an uncertain pair for review. Never overwrites a human decision."""
    existing = get_decision(engine, kalshi_market_id)
    if existing is not None and existing[0] in ("approved", "blocked"):
        return
    _upsert(
        engine, kalshi_market_id,
        poly_condition_id=poly_condition_id,
        status="pending",
        similarity=similarity,
        kalshi_title=kalshi_title,
        poly_question=poly_question,
        verdict=verdict,
        reason=reason,
    )


def record_auto_approved(
    engine: Engine,
    kalshi_market_id: str,
    poly_condition_id: str,
    similarity: float,
    kalshi_title: str,
    poly_question: str,
) -> None:
    """Remember a pair every extracted field agreed on, so matching runs once."""
    existing = get_decision(engine, kalshi_market_id)
    if existing is not None and existing[0] == "blocked":
        return  # a human said no; an entity match does not overrule that
    _upsert(
        engine, kalshi_market_id,
        poly_condition_id=poly_condition_id,
        status="approved",
        similarity=similarity,
        kalshi_title=kalshi_title,
        poly_question=poly_question,
        verdict="match",
        reason="all extracted fields agree",
        decided_by="entity_match",
        decided_at=datetime.now(timezone.utc),
    )


def decide(engine: Engine, match_id: int, status: str, decided_by: str = "human") -> bool:
    """Apply a human approve/block to a queued pair."""
    if status not in ("approved", "blocked"):
        raise ValueError(f"invalid status: {status}")
    with get_session(engine) as session:
        row = session.get(MarketMatchMap, match_id)
        if row is None:
            return False
        row.status = status
        row.decided_by = decided_by
        row.decided_at = datetime.now(timezone.utc)
        session.commit()
        logger.info(f"Match {match_id} ({row.kalshi_market_id}) marked {status}")
        return True


def list_matches(engine: Engine, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_session(engine) as session:
        query = session.query(MarketMatchMap)
        if status:
            query = query.filter(MarketMatchMap.status == status)
        rows = query.order_by(MarketMatchMap.created_at.desc()).all()
        return [
            {
                "id": r.id,
                "kalshi_market_id": r.kalshi_market_id,
                "poly_condition_id": r.poly_condition_id,
                "status": r.status,
                "similarity": r.similarity,
                "kalshi_title": r.kalshi_title,
                "poly_question": r.poly_question,
                "verdict": r.verdict,
                "reason": r.reason,
                "decided_by": r.decided_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
