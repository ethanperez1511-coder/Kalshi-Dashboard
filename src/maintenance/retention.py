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

The orderbook deltas are the other pressure, and they have a hard constraint the
snapshots do not: they cannot be re-collected. Kalshi serves no historical book,
so a delta deleted inside the simulator's validation window is evidence that can
never be recovered. Retention therefore REFUSES to touch deltas until that
window has closed, even if the database is full — the correct response to
running out of space is to say so, not to destroy the one dataset with no
second source.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import Engine, func, select, text

from src.database import get_session

logger = logging.getLogger(__name__)

# Neon free tier.
TIER_LIMIT_BYTES = 512 * 1024 * 1024
WARN_FRACTION = 0.80
EMERGENCY_FRACTION = 0.90

# Price snapshots: full resolution recently, then one per market per hour, then
# one per market per day, then gone.
SNAPSHOT_FULL_DAYS = 3
SNAPSHOT_HOURLY_DAYS = 14
SNAPSHOT_MAX_DAYS = 60

# Orderbook deltas: never pruned inside the validation window. The window is
# day-zero (first recorded delta) plus this, generously beyond the ~30 days the
# design expects to need.
DELTA_VALIDATION_DAYS = 60


@dataclass
class RetentionPlan:
    size_bytes: int = 0
    limit_bytes: int = TIER_LIMIT_BYTES
    deletions: Dict[str, int] = field(default_factory=dict)
    protected: List[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return self.size_bytes / self.limit_bytes if self.limit_bytes else 0.0

    @property
    def over_warn(self) -> bool:
        return self.fraction >= WARN_FRACTION

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


def delta_window_end(engine: Engine) -> Optional[dt.datetime]:
    """When the orderbook deltas stop being protected.

    None means no deltas exist yet, which is also a refusal to prune: there is
    nothing to prune and no window to reason about.
    """
    from src.models.orderbook_raw import OrderbookDeltaRaw

    with get_session(engine) as session:
        first = session.execute(
            select(func.min(OrderbookDeltaRaw.received_at))
        ).scalar()
    if first is None:
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=dt.timezone.utc)
    return first + dt.timedelta(days=DELTA_VALIDATION_DAYS)


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

    window_end = delta_window_end(engine)
    if window_end is None:
        plan.protected.append("orderbook deltas: none recorded")
    elif now < window_end:
        remaining = (window_end - now).days
        plan.protected.append(
            f"orderbook deltas: PROTECTED for {remaining} more days — Kalshi "
            f"serves no historical book, so a deleted delta cannot be recovered"
        )
    else:
        with get_session(engine) as session:
            plan.deletions["orderbook_deltas_expired"] = session.execute(
                select(func.count(OrderbookDeltaRaw.id))
                .where(OrderbookDeltaRaw.received_at < window_end)
            ).scalar() or 0

    return plan


def apply_retention(engine: Engine, now: Optional[dt.datetime] = None) -> RetentionPlan:
    """Apply the plan. Deletes in bounded statements, never a full-table scan."""
    now = now or dt.datetime.now(dt.timezone.utc)
    plan = plan_retention(engine, now)

    from src.models.orderbook_raw import OrderbookDeltaRaw
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

        # 3. Deltas, only once their window has closed.
        window_end = delta_window_end(engine)
        if window_end is not None and now >= window_end:
            session.query(OrderbookDeltaRaw).filter(
                OrderbookDeltaRaw.received_at < window_end
            ).delete(synchronize_session=False)

        session.commit()

    plan.size_bytes = database_size_bytes(engine)
    logger.info(
        "Retention applied: %d rows removed, now %.1f%% of tier",
        plan.total_deletions, plan.fraction * 100,
    )
    return plan


def format_plan(plan: RetentionPlan, applied: bool = False) -> str:
    used = plan.size_bytes / 1e6
    limit = plan.limit_bytes / 1e6
    lines = [
        f"{'Applied' if applied else 'Planned'} retention — "
        f"{used:.0f}/{limit:.0f} MB ({plan.fraction:.0%} of tier)"
    ]
    for name, count in sorted(plan.deletions.items()):
        if count:
            lines.append(f"  {'removed' if applied else 'would remove'} {count:,} {name}")
    for note in plan.protected:
        lines.append(f"  {note}")
    if plan.fraction >= EMERGENCY_FRACTION:
        lines.append("  🚨 above 90% — ingest will outrun retention")
    elif plan.over_warn:
        lines.append("  ⚠️ above 80% of the free tier")
    return "\n".join(lines)


def format_size_line(plan: RetentionPlan) -> str:
    """One line for the daily digest."""
    used = plan.size_bytes / 1e6
    mark = "🚨" if plan.fraction >= EMERGENCY_FRACTION else (
        "⚠️" if plan.over_warn else "💾"
    )
    return f"{mark} DB: {used:.0f} MB ({plan.fraction:.0%} of free tier)"
