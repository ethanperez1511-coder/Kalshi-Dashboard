"""Keep the database under the free tier, indefinitely.

The arithmetic that forces this: ingest writes ~5,084 price snapshots per
5-minute cycle, which is 1.46M rows/day. At the ~436 bytes/row measured on the
existing database that fills Neon's 0.5 GB tier in **2.8 days**. Pruning alone
cannot fix a write rate that high, so there are two halves:

  SOURCE     stop writing rows nobody will ever read (see price_recorder) —
             ~69% of snapshots are for markets with no trade history, which the
             scorer skips outright.
  RETENTION  age out what remains, keeping recent data at full resolution and
             older data at reducing resolution.

The orderbook tape is the other pressure. It cannot be re-collected, because
Kalshi serves no historical book, so it is kept for a ROLLING window of
DELTA_RETENTION_DAYS measured back from now. That window covers everything
replay and shadow read.

It used to be anchored to the first recorded delta, `min(received_at) + 60 d`.
That protected everything for sixty days, which at the measured +49.5 MB/day is
about 3 GB against the cap. On day sixty it then deleted the whole tape in one
statement, recent days included, and re-anchored. The database hit the cap on
2026-09-11 and Neon refused every write, retention's own DELETEs included,
until the cap was raised on 2026-10-01: twenty days dark. Ruling 2026-10-06:
the window rolls.

Two sizes, kept apart on purpose:

  NEON_CAP_BYTES        the provider's hard limit. At it, writes AND deletes
                        are refused and cleanup deadlocks. A fact, not a choice.
  STORAGE_BUDGET_BYTES  our target, well under the cap. Retention goes red when
                        it cannot hold this, so the warning arrives with half
                        the cap still free. September's lesson is headroom.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import Engine, func, select, text

from src.database import get_session

logger = logging.getLogger(__name__)

# Decimal, as the Neon console reports. The heartbeat and the console must
# show the same number (762 MB in the console was 721 "MB" in MiB here).
MB = 1_000_000

# Neon Free plan cap, raised from 0.5 GB to 1 GB on 2026-10-02 (changelog).
# Taken as DECIMAL GB, the smaller reading: the console counts in decimal
# units, and if the cap is in fact 1 GiB this only errs early.
NEON_CAP_BYTES = 1000 * MB
# Our own ceiling. Red above this, after pruning.
STORAGE_BUDGET_BYTES = 500 * MB
# Fraction of the CAP at which the digest escalates to 🚨. Past here the next
# few days of growth can deadlock cleanup.
CAP_ALARM_FRACTION = 0.85

# Price snapshots: full resolution recently, then one per market per hour, then
# one per market per day, then gone.
SNAPSHOT_FULL_DAYS = 3
SNAPSHOT_HOURLY_DAYS = 14
SNAPSHOT_MAX_DAYS = 60

# Orderbook tape (deltas, snapshots, trade prints): kept for this many days
# measured back from NOW. It equals the compaction and replay window, so
# nothing that replay or shadow reads is ever pruned.
DELTA_RETENTION_DAYS = 14

# Rows per DELETE. A killed run leaves a smaller job behind, not a rolled-back
# hour.
DELETE_BATCH = 10_000

# Tables retention deletes from. Plain VACUUM runs on these after every pass so
# freed pages are reused instead of the files growing.
PRUNED_TABLES = ("price_snapshots", "orderbook_delta_raw")


@dataclass
class RetentionPlan:
    size_bytes: int = 0
    deletions: Dict[str, int] = field(default_factory=dict)
    protected: List[str] = field(default_factory=list)

    @property
    def cap_fraction(self) -> float:
        return self.size_bytes / NEON_CAP_BYTES

    @property
    def over_budget(self) -> bool:
        return self.size_bytes > STORAGE_BUDGET_BYTES

    @property
    def cap_alarm(self) -> bool:
        return self.cap_fraction >= CAP_ALARM_FRACTION

    @property
    def total_deletions(self) -> int:
        return sum(self.deletions.values())


def database_size_bytes(engine: Engine) -> int:
    """Actual size on disk, for whichever backend is in use."""
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            return int(conn.execute(
                text("SELECT pg_database_size(current_database())")
            ).scalar() or 0)

    # SQLite: page_count * page_size is exact and cheap.
    with engine.connect() as conn:
        pages = conn.execute(text("PRAGMA page_count")).scalar() or 0
        page_size = conn.execute(text("PRAGMA page_size")).scalar() or 0
    return int(pages) * int(page_size)


def tape_cutoff(now: dt.datetime) -> dt.datetime:
    """Tape received before this is pruned. Rolls with the clock and never
    depends on what is in the table."""
    return now - dt.timedelta(days=DELTA_RETENTION_DAYS)


def plan_retention(engine: Engine, now: Optional[dt.datetime] = None) -> RetentionPlan:
    """What would be deleted. Counts only — no writes."""
    now = now or dt.datetime.now(dt.timezone.utc)
    plan = RetentionPlan(size_bytes=database_size_bytes(engine))

    from src.models.orderbook_raw import OrderbookDeltaRaw
    from src.models.price import PriceSnapshot

    full_before = now - dt.timedelta(days=SNAPSHOT_FULL_DAYS)
    hourly_before = now - dt.timedelta(days=SNAPSHOT_HOURLY_DAYS)
    max_before = now - dt.timedelta(days=SNAPSHOT_MAX_DAYS)

    with get_session(engine) as session:
        plan.deletions["price_snapshots_expired"] = session.execute(
            select(func.count(PriceSnapshot.id))
            .where(PriceSnapshot.timestamp < max_before)
        ).scalar() or 0

        # Thinning candidates: everything past full resolution that is not the
        # newest row for its market in its bucket. Counted approximately here
        # and applied exactly in apply_retention.
        plan.deletions["price_snapshots_thinned"] = session.execute(
            select(func.count(PriceSnapshot.id))
            .where(PriceSnapshot.timestamp < full_before)
            .where(PriceSnapshot.timestamp >= max_before)
        ).scalar() or 0

        plan.deletions["orderbook_tape_expired"] = session.execute(
            select(func.count(OrderbookDeltaRaw.id))
            .where(OrderbookDeltaRaw.received_at < tape_cutoff(now))
        ).scalar() or 0

    plan.protected.append(
        f"orderbook tape: last {DELTA_RETENTION_DAYS} days kept (rolling) — "
        f"Kalshi serves no historical book, so this is the only copy"
    )
    return plan


def prune_tape(engine: Engine, now: dt.datetime) -> int:
    """Delete tape older than the rolling window, DELETE_BATCH rows at a time.

    Each batch commits on its own, so a run killed halfway has still removed
    what it got through. It selects by the indexed received_at, so no batch
    scans the table.
    """
    from src.models.orderbook_raw import OrderbookDeltaRaw

    cutoff = tape_cutoff(now)
    deleted = 0
    while True:
        with get_session(engine) as session:
            ids = [
                row[0] for row in session.execute(
                    select(OrderbookDeltaRaw.id)
                    .where(OrderbookDeltaRaw.received_at < cutoff)
                    .limit(DELETE_BATCH)
                ).all()
            ]
            if not ids:
                return deleted
            result = session.execute(
                OrderbookDeltaRaw.__table__.delete()
                .where(OrderbookDeltaRaw.id.in_(ids))
            )
            session.commit()
            deleted += result.rowcount or 0


def vacuum_pruned_tables(engine: Engine) -> List[str]:
    """Plain VACUUM (ANALYZE) on the tables retention deletes from.

    DELETE only marks rows dead. VACUUM makes their space reusable, so the
    next day's writes fill it instead of extending the files. Autovacuum would
    do this eventually, but only while the compute is awake, and on a
    scale-to-zero database that is not often enough to count on. Plain VACUUM
    takes no exclusive lock, so the recorder and trade cycle keep running.
    It does NOT shrink the files: that takes VACUUM FULL, which is a separate,
    token-gated maintenance action.
    """
    if engine.dialect.name != "postgresql":
        return []
    # VACUUM cannot run inside a transaction block.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in PRUNED_TABLES:
            conn.execute(text(f"VACUUM (ANALYZE) {table}"))
    return list(PRUNED_TABLES)


def apply_retention(engine: Engine, now: Optional[dt.datetime] = None) -> RetentionPlan:
    """Apply the plan. Deletes in bounded statements, never a full-table scan."""
    now = now or dt.datetime.now(dt.timezone.utc)
    plan = plan_retention(engine, now)

    from src.models.price import PriceSnapshot

    full_before = now - dt.timedelta(days=SNAPSHOT_FULL_DAYS)
    max_before = now - dt.timedelta(days=SNAPSHOT_MAX_DAYS)

    with get_session(engine) as session:
        # 1. Hard expiry.
        session.query(PriceSnapshot).filter(
            PriceSnapshot.timestamp < max_before
        ).delete(synchronize_session=False)

        # 2. Thin older snapshots to one row per market per hour, keeping the
        # newest in each bucket. Done by id so the survivor is deterministic.
        keep = select(func.max(PriceSnapshot.id)).where(
            PriceSnapshot.timestamp < full_before,
            PriceSnapshot.timestamp >= max_before,
        ).group_by(
            PriceSnapshot.market_id,
            func.strftime("%Y-%m-%dT%H", PriceSnapshot.timestamp)
            if engine.dialect.name == "sqlite"
            else func.date_trunc("hour", PriceSnapshot.timestamp),
        )
        session.query(PriceSnapshot).filter(
            PriceSnapshot.timestamp < full_before,
            PriceSnapshot.timestamp >= max_before,
            ~PriceSnapshot.id.in_(keep),
        ).delete(synchronize_session=False)

        session.commit()

    # 3. Tape older than the rolling window, in batches.
    plan.deletions["orderbook_tape_expired"] = prune_tape(engine, now)

    plan.size_bytes = database_size_bytes(engine)
    logger.info(
        "Retention applied: %d rows removed, now %.0f MB (%.0f%% of cap)",
        plan.total_deletions, plan.size_bytes / MB, plan.cap_fraction * 100,
    )
    return plan


def size_status(size_bytes: int) -> str:
    """Budget and cap in one phrase, so neither number appears alone."""
    return (
        f"{size_bytes / MB:.0f} MB — {size_bytes / STORAGE_BUDGET_BYTES:.0%} of "
        f"{STORAGE_BUDGET_BYTES / MB:.0f} MB budget, "
        f"{size_bytes / NEON_CAP_BYTES:.0%} of {NEON_CAP_BYTES / MB:.0f} MB Neon cap"
    )


def format_plan(plan: RetentionPlan, applied: bool = False) -> str:
    lines = [
        f"{'Applied' if applied else 'Planned'} retention — {size_status(plan.size_bytes)}"
    ]
    for name, count in sorted(plan.deletions.items()):
        if count:
            lines.append(f"  {'removed' if applied else 'would remove'} {count:,} {name}")
    for note in plan.protected:
        lines.append(f"  {note}")
    if plan.cap_alarm:
        lines.append(
            f"  🚨 at {plan.cap_fraction:.0%} of the Neon cap — at 100% Neon "
            f"refuses writes AND deletes, and retention cannot run"
        )
    elif plan.over_budget:
        lines.append(
            "  ⚠️ over budget after pruning. If the live rows fit, the space is "
            "dead pages: dispatch maintenance vacuum_full. If not, the write "
            "rate is the problem"
        )
    return "\n".join(lines)


def format_size_line(plan: RetentionPlan) -> str:
    """One line for the daily digest."""
    mark = "🚨" if plan.cap_alarm else ("⚠️" if plan.over_budget else "💾")
    return f"{mark} DB: {size_status(plan.size_bytes)}"
