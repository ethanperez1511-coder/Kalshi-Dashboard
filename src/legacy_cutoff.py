"""Separate trades placed by deployed current code from those that predate it.

Thirteen commits sat unpushed while production ran July code. The paper record
on Neon was therefore produced by a system that no longer exists: broken auth,
fee-blind settlement, and the old Polymarket matcher that accepted
horizon-mismatched pairs.

The 50-trade gate exists to evaluate the system that would go live. A trade
placed by superseded code is not evidence about that system, so it is excluded —
from the counter and from calibration fits alike. The rows stay: history is
kept, only its standing changes.

The cutoff is the deploy SHA, not a timestamp. A clock cutoff would silently
misclassify anything placed while the deploy was mid-rollout; a SHA states
exactly which code produced the row.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.trade import Trade

logger = logging.getLogger(__name__)


def current_deploy_sha() -> Optional[str]:
    return os.environ.get("GITHUB_SHA") or None


def mark_legacy_trades(engine: Engine) -> int:
    """Mark every trade lacking a deploy SHA as legacy. Idempotent.

    A missing SHA means the row was written before deploy tracking existed,
    which is exactly the population that predates the push.
    """
    with get_session(engine) as session:
        rows = session.query(Trade).filter(
            Trade.deploy_sha.is_(None), Trade.is_legacy.is_(False)
        ).all()
        for row in rows:
            row.is_legacy = True
        session.commit()
        count = len(rows)
    if count:
        logger.info(
            "Marked %d trades legacy: placed before the deploy cutoff, excluded "
            "from the gate and from calibration", count,
        )
    return count


def gate_count(engine: Engine) -> int:
    """Paper trades that count toward the 50-trade gate."""
    with get_session(engine) as session:
        return session.execute(
            select(func.count(Trade.id))
            .where(Trade.is_paper.is_(True))
            .where(Trade.is_legacy.is_(False))
        ).scalar() or 0


def legacy_count(engine: Engine) -> int:
    with get_session(engine) as session:
        return session.execute(
            select(func.count(Trade.id)).where(Trade.is_legacy.is_(True))
        ).scalar() or 0


def resync_gate_counter(engine: Engine) -> int:
    """Point TradingSettings.paper_trade_count at the non-legacy count.

    The counter is what `can_trade_live` reads, so it must agree with the rows
    rather than drift from them.
    """
    from src.models.settings import TradingSettings

    count = gate_count(engine)
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        if settings is not None and settings.paper_trade_count != count:
            logger.info(
                "Gate counter resynced: %s -> %d (legacy trades excluded)",
                settings.paper_trade_count, count,
            )
            settings.paper_trade_count = count
        session.commit()
    return count


def suspect_open_positions(engine: Engine) -> List[dict]:
    """Open positions whose entry price came from a structurally biased model.

    The Polymarket horizon mismatch biases p_model in one direction only: the
    short-dated contract's probability is bounded above by the long-dated one,
    so YES is understated and NO looks cheap. Every position entered that way is
    holding the wrong side of a bias that will not mean-revert.

    Flagged for review rather than closed automatically — unwinding is a
    position decision, and this code does not get to make those.
    """
    from src.models.position import Position

    with get_session(engine) as session:
        open_markets = {
            row[0] for row in session.execute(
                select(Position.market_id).where(Position.status == "open")
            ).all()
        }
        rows = session.execute(
            select(Trade.market_id, Trade.side, Trade.price, Trade.quantity,
                   Trade.p_model, Trade.reasoning, Trade.is_legacy)
            .where(Trade.market_id.in_(open_markets))
        ).all() if open_markets else []

    suspects: List[dict] = []
    seen = set()
    for market_id, side, price, quantity, p_model, reasoning, is_legacy in rows:
        if market_id in seen:
            continue
        text = (reasoning or "").lower()
        if "polymarket" not in text:
            continue
        seen.add(market_id)
        suspects.append({
            "market_id": market_id,
            "side": side,
            "entry_price": price,
            "quantity": quantity,
            "p_model": p_model,
            "legacy": bool(is_legacy),
            "exposure": round(price * quantity / 100.0, 2),
            "reason": (
                "entered on a Polymarket-sourced p_model; the horizon mismatch "
                "understates YES, so a NO entry is holding the biased side"
            ),
        })
    return suspects
