"""Null delta payloads outside the replay window. Never inside it.

`orderbook_delta_raw.payload` stores the complete raw JSON of every message, on
the stated principle that reconstruction logic will change as the simulator is
built and corrected, so re-deriving from the original message must always be
possible.

For DELTA messages that principle stops paying after a point. The row already
denormalises market_ticker, sid, seq, side, price_dollars, delta_fp and ts_ms —
which is the entire content of a delta message — so keeping the JSON alongside
roughly doubles the write for no information.

But it pays for everything INSIDE the window replay and shadow actually read,
and that carve-out is the design rather than a detail: payloads stay full for
COMPACTION_WINDOW_DAYS and are nulled only after. Trade prints keep theirs for
the full retention period regardless — `fill_sim` reads taker_outcome_side,
count_fp and is_block_trade from the payload and none of those have columns.
Snapshots keep theirs because a delta stream without its anchoring snapshot
reconstructs nothing.

This reduces STORAGE and future write volume. It does not reduce transfer on
its own: nothing reads the tape per cycle any more.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import Engine, and_, func, select

from src.database import get_session
from src.models.orderbook_raw import OrderbookDeltaRaw

logger = logging.getLogger(__name__)

# Replay and shadow consume recent tape. Inside this window the raw message
# stays, because re-derivability is load-bearing exactly there.
COMPACTION_WINDOW_DAYS = 14

# Message types whose payload is NEVER nulled, at any age.
PAYLOAD_ALWAYS_KEPT = ("trade", "snapshot")

BATCH = 5_000


@dataclass
class CompactionPlan:
    now: dt.datetime
    cutoff: dt.datetime
    deltas_to_null: int = 0
    bytes_reclaimed: int = 0
    protected_recent: int = 0
    protected_by_type: int = 0


def plan_compaction(
    engine: Engine, now: Optional[dt.datetime] = None,
) -> CompactionPlan:
    """Count what would be nulled. Reads only."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=COMPACTION_WINDOW_DAYS)
    plan = CompactionPlan(now=now, cutoff=cutoff)

    stale_deltas = and_(
        OrderbookDeltaRaw.msg_type.notin_(PAYLOAD_ALWAYS_KEPT),
        OrderbookDeltaRaw.received_at < cutoff,
        OrderbookDeltaRaw.payload.isnot(None),
    )

    with get_session(engine) as session:
        plan.deltas_to_null, plan.bytes_reclaimed = session.execute(
            select(
                func.count(OrderbookDeltaRaw.id),
                func.coalesce(func.sum(func.length(OrderbookDeltaRaw.payload)), 0),
            ).where(stale_deltas)
        ).one()

        plan.protected_recent = session.execute(
            select(func.count(OrderbookDeltaRaw.id)).where(
                OrderbookDeltaRaw.received_at >= cutoff
            )
        ).scalar() or 0

        plan.protected_by_type = session.execute(
            select(func.count(OrderbookDeltaRaw.id)).where(
                OrderbookDeltaRaw.msg_type.in_(PAYLOAD_ALWAYS_KEPT)
            )
        ).scalar() or 0

    return plan


def compact_tape(
    engine: Engine, plan: CompactionPlan, now: Optional[dt.datetime] = None,
) -> dict:
    """Apply the plan. Nulls delta payloads older than the window."""
    now = now or dt.datetime.now(dt.timezone.utc)
    nulled = 0

    while True:
        with get_session(engine) as session:
            ids = [
                row[0] for row in session.execute(
                    select(OrderbookDeltaRaw.id)
                    .where(
                        OrderbookDeltaRaw.msg_type.notin_(PAYLOAD_ALWAYS_KEPT),
                        OrderbookDeltaRaw.received_at < plan.cutoff,
                        OrderbookDeltaRaw.payload.isnot(None),
                    )
                    .limit(BATCH)
                ).all()
            ]
            if not ids:
                break
            result = session.execute(
                OrderbookDeltaRaw.__table__.update()
                .where(OrderbookDeltaRaw.id.in_(ids))
                .values(payload=None)
            )
            session.commit()
            nulled += result.rowcount or 0

    logger.info(
        "Tape compaction: nulled %d delta payloads older than %d days",
        nulled, COMPACTION_WINDOW_DAYS,
    )
    return {"nulled": nulled, "bytes_reclaimed": plan.bytes_reclaimed}


def format_compaction(plan: CompactionPlan, executed: Optional[dict] = None) -> str:
    mb = plan.bytes_reclaimed / 1_048_576
    lines = [
        "TAPE COMPACTION " + ("EXECUTED" if executed else "DRY RUN"),
        "",
        f"  window                  : {COMPACTION_WINDOW_DAYS} days "
        f"(cutoff {plan.cutoff:%Y-%m-%d %H:%M}Z)",
        f"  delta payloads to null  : {plan.deltas_to_null:,}  (~{mb:.1f} MB)",
        f"  protected, inside window: {plan.protected_recent:,}",
        f"  protected, trade/snapshot: {plan.protected_by_type:,}",
        "",
        "  Deltas keep every reconstruction field in their own columns —",
        "  ticker, sid, seq, side, price, delta, ts_ms — so the payload is",
        "  redundant once outside the replay window. trade and snapshot",
        "  payloads are NEVER nulled: fill_sim reads taker_outcome_side,",
        "  count_fp and is_block_trade from them, and a delta stream without",
        "  its anchoring snapshot reconstructs nothing.",
    ]
    if executed:
        lines.append(f"\n  RESULT: {executed['nulled']:,} payloads nulled")
    return "\n".join(lines)
