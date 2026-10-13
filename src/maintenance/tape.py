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
COMPACTION_WINDOW_DAYS and are nulled only after. Ruling 2026-10-06 (a) cut
that window from 14 days to 2. Fourteen days of full payloads weighed ~640 MB
at the measured 80k rows/day, over the 500 MB budget and heading for the cap,
and past two days a delta's JSON only repeats its own columns (replay reads
the columns when the payload is gone). Trade prints keep theirs for
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
from typing import Callable, Optional

from sqlalchemy import Engine, and_, func, select, text

from src.database import get_session
from src.maintenance.retention import NEON_CAP_BYTES, database_size_bytes
from src.models.orderbook_raw import OrderbookDeltaRaw

logger = logging.getLogger(__name__)

# Replay and shadow consume recent tape. Inside this window the raw message
# stays, because re-derivability is load-bearing exactly there.
COMPACTION_WINDOW_DAYS = 2

# Message types whose payload is NEVER nulled, at any age.
PAYLOAD_ALWAYS_KEPT = ("trade", "snapshot")

# Rows per UPDATE transaction (ruling: at most 20,000). Each commits alone, so a
# killed run keeps what it finished and loses nothing it had not.
BATCH = 20_000
# Each UPDATE writes a new row version before VACUUM reclaims the old one, so
# the pass itself adds storage. It stops before the cap can be approached.
# Below this the pass runs, in BATCH-row steps with VACUUM after each. It was
# 0.90 until 2026-10-13, when it blocked the one step that frees space at a
# 903 MB file holding 206 MB reusable (ruling: allow it below 95%).
CAP_STOP_FRACTION = 0.95
# An UPDATE writes a new row version before the old one is reclaimed. With
# little free space (46 MB in production on 2026-10-12) a single long pass
# would grow the file by the size of every new version. A plain VACUUM every
# few batches frees the old versions so the next batches reuse their space.
VACUUM_EVERY_BATCHES = 1
# Rows nulled per run. Each nulled row adds a new entry to all four indexes
# before VACUUM can reclaim the old ones: measured on Postgres 16, 432k rows
# grew a 313 MB table by 44 MB even with VACUUM after every batch. Production
# had ~765k rows of backlog and 46 MB of free space at 807 MB (2026-10-12), so
# one pass would end near the 90% line. Capped, the backlog clears over a few
# daily runs at ~+20 MB each; the steady daily flow (~70k) sits well under it.
MAX_ROWS_PER_RUN = 200_000


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
    _before_commit: Optional[Callable[[object], None]] = None,
) -> dict:
    """Apply the plan. Nulls delta payloads older than the window."""
    now = now or dt.datetime.now(dt.timezone.utc)
    nulled = 0
    batches = 0
    stopped = ""

    while True:
        if nulled >= MAX_ROWS_PER_RUN:
            stopped = f"run cap: {MAX_ROWS_PER_RUN:,} rows nulled; the rest continues next run"
            logger.info("Tape compaction: %s", stopped)
            break
        if database_size_bytes(engine) >= CAP_STOP_FRACTION * NEON_CAP_BYTES:
            stopped = f"cap guard: database at {CAP_STOP_FRACTION:.0%} of the Neon cap"
            logger.error("Tape compaction stopped: %s", stopped)
            break
        with get_session(engine) as session:
            # One statement per batch, ids chosen by the server: no id list
            # crosses the wire and no bind-parameter count grows with BATCH.
            batch = (
                select(OrderbookDeltaRaw.id)
                .where(
                    OrderbookDeltaRaw.msg_type.notin_(PAYLOAD_ALWAYS_KEPT),
                    OrderbookDeltaRaw.received_at < plan.cutoff,
                    OrderbookDeltaRaw.payload.isnot(None),
                )
                .limit(min(BATCH, MAX_ROWS_PER_RUN - nulled))
                .scalar_subquery()
            )
            result = session.execute(
                OrderbookDeltaRaw.__table__.update()
                .where(OrderbookDeltaRaw.id.in_(batch))
                .values(payload=None)
            )
            if not result.rowcount:
                break
            if _before_commit is not None:
                _before_commit(session)          # test hook: a kill mid-batch
            session.commit()
            nulled += result.rowcount or 0
            batches += 1
        if batches % VACUUM_EVERY_BATCHES == 0 and engine.dialect.name == "postgresql":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("VACUUM orderbook_delta_raw"))

    logger.info(
        "Tape compaction: nulled %d delta payloads older than %d days",
        nulled, COMPACTION_WINDOW_DAYS,
    )
    return {"nulled": nulled, "bytes_reclaimed": plan.bytes_reclaimed, "stopped": stopped}


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
        if executed.get("stopped"):
            lines.append(f"  STOPPED: {executed['stopped']}")
    return "\n".join(lines)
