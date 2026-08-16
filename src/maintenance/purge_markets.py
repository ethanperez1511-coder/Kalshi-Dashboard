"""Reclaim the parlay graveyard. Dry run by default, token to execute.

Measured 2026-08-17: 374,000 of 376,000 rows counted as open markets were
KXMVECROSSCATEGORY and KXMVESPORTSMULTIGAMEEXTENDED parlay mints. `exclusions`
stops new ones arriving; this removes the ones already on disk, because at
~60 MB/day against 126 MB of headroom stopping the inflow alone still ran out
of tier.

The standing rule is archive, never delete. The amendment that makes deletion
possible here is narrow and was stated explicitly: a market row with ZERO
dependent data — no price snapshot, no trade, no position, no opportunity, no
recorded book delta — is noise, not history. Nothing the backtester or the
day-7 measurement could ever read is attached to it. Deleting it loses nothing
that ever existed.

So there are three outcomes, and every candidate lands in exactly one:

  EXEMPT      an open position, or an opportunity inside the recorder's
              recency window. Untouched entirely — not deleted, not archived.
              `markets_to_record` joins `markets` and requires an open status,
              so archiving one of these rows blinds the book recorder on a
              market it is taping right now, and the book cannot be backfilled.
  ARCHIVABLE  has dependent rows. The row stays and keeps every join intact;
              only its status changes, which takes it out of the open universe.
  DELETABLE   no dependents at all. Removed.

A market that is still live and not in an excluded series is not a candidate at
all, whatever its dependents look like.

Postgres does not return deleted space to the tier on DELETE alone — the pages
are marked reusable, which stops growth but does not shrink
`pg_database_size`. Since this removes the overwhelming majority of the table,
`VACUUM FULL` rewrites it into a small new heap and genuinely reclaims. It
takes an ACCESS EXCLUSIVE lock for the duration, so a five-minute trading cycle
overlapping it will block or fail one tick. That is deliberate and stated
rather than hidden: the alternative is a database that stays full.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Engine, and_, func, not_, or_, select, text

from src.database import get_session
from src.ingestion.exclusions import EXCLUDED_SERIES, series_of
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.trade import Trade
from src.recorder.book_recorder import RECENT_OPPORTUNITY_HOURS

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "PURGE-ORPHAN-MARKETS"

# Status given to a candidate that has dependent rows. Not in OPEN_STATUSES, so
# it leaves the funnel's denominator and the recorder's subscribe list without
# losing the row any dependent still points at.
ARCHIVED_STATUS = "archived"

DELETE_BATCH = 5_000


@dataclass
class PurgePlan:
    now: dt.datetime
    deletable: int = 0
    archivable: int = 0
    exempt: int = 0
    deletable_by_series: List[Tuple[str, int]] = field(default_factory=list)
    archivable_by_series: List[Tuple[str, int]] = field(default_factory=list)
    size_before: Optional[int] = None
    avg_market_row_bytes: Optional[float] = None
    estimated_reclaim_bytes: Optional[int] = None
    _delete_ids: List[str] = field(default_factory=list, repr=False)
    _archive_ids: List[str] = field(default_factory=list, repr=False)


def _dependent_ids(session) -> set:
    """Every market_id referenced by anything worth keeping."""
    referenced = set()
    for column in (
        PriceSnapshot.market_id,
        Trade.market_id,
        Position.market_id,
        Opportunity.market_id,
        OrderbookDeltaRaw.market_ticker,
    ):
        referenced.update(
            row[0] for row in session.execute(select(column).distinct()).all() if row[0]
        )
    return referenced


def _exempt_ids(session, now: dt.datetime) -> set:
    """Markets nothing may touch: open exposure, or actively being recorded."""
    exempt = {
        row[0] for row in session.execute(
            select(Position.market_id).where(Position.status == "open").distinct()
        ).all() if row[0]
    }
    exempt.update(
        row[0] for row in session.execute(
            select(Opportunity.market_id).where(
                Opportunity.scored_at
                >= now - dt.timedelta(hours=RECENT_OPPORTUNITY_HOURS)
            ).distinct()
        ).all() if row[0]
    )
    return exempt


def _database_size(engine: Engine) -> Optional[int]:
    if engine.dialect.name != "postgresql":
        return None
    with get_session(engine) as session:
        return session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar_one()


def _avg_market_row_bytes(engine: Engine) -> Optional[float]:
    """Measured, not assumed: parlay titles run to ~1,400 characters."""
    if engine.dialect.name != "postgresql":
        return None
    with get_session(engine) as session:
        total, rows = session.execute(
            text(
                "SELECT pg_total_relation_size('markets'), "
                "(SELECT count(*) FROM markets)"
            )
        ).one()
    return (float(total) / rows) if rows else None


def plan_purge(engine: Engine, now: Optional[dt.datetime] = None) -> PurgePlan:
    """Classify every candidate. Reads only — changes nothing."""
    now = now or dt.datetime.now(dt.timezone.utc)
    plan = PurgePlan(now=now)

    with get_session(engine) as session:
        # Candidates: an excluded series (however live — no model will price
        # it), or any market that has passed its own close date.
        candidates = session.execute(
            select(Market.market_id, Market.status, Market.close_date)
        ).all()

        referenced = _dependent_ids(session)
        exempt = _exempt_ids(session, now)

    deletable: Dict[str, int] = {}
    archivable: Dict[str, int] = {}

    for market_id, status, close_date in candidates:
        series = series_of(market_id)
        past_close = close_date is not None and _aware(close_date) < now
        if series not in EXCLUDED_SERIES and not past_close:
            continue                       # still live and still priceable
        if status == ARCHIVED_STATUS:
            continue                       # already done on a previous run

        if market_id in exempt:
            plan.exempt += 1
            continue

        if market_id in referenced:
            plan.archivable += 1
            archivable[series] = archivable.get(series, 0) + 1
            plan._archive_ids.append(market_id)
        else:
            plan.deletable += 1
            deletable[series] = deletable.get(series, 0) + 1
            plan._delete_ids.append(market_id)

    plan.deletable_by_series = sorted(deletable.items(), key=lambda kv: -kv[1])
    plan.archivable_by_series = sorted(archivable.items(), key=lambda kv: -kv[1])
    plan.size_before = _database_size(engine)
    plan.avg_market_row_bytes = _avg_market_row_bytes(engine)
    if plan.avg_market_row_bytes:
        plan.estimated_reclaim_bytes = int(plan.avg_market_row_bytes * plan.deletable)
    return plan


def _aware(stamp: dt.datetime) -> dt.datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def execute_purge(
    engine: Engine, plan: PurgePlan, now: Optional[dt.datetime] = None,
    vacuum: bool = True,
) -> Dict[str, object]:
    """Apply the plan. Deletes orphans, archives the rest, then reclaims."""
    now = now or dt.datetime.now(dt.timezone.utc)
    deleted = 0

    # Batched so a mid-run kill leaves a smaller job behind rather than a
    # rolled-back hour and a still-full database.
    for start in range(0, len(plan._delete_ids), DELETE_BATCH):
        batch = plan._delete_ids[start:start + DELETE_BATCH]
        with get_session(engine) as session:
            result = session.execute(
                Market.__table__.delete().where(Market.market_id.in_(batch))
            )
            session.commit()
            deleted += result.rowcount or 0

    archived = 0
    for start in range(0, len(plan._archive_ids), DELETE_BATCH):
        batch = plan._archive_ids[start:start + DELETE_BATCH]
        with get_session(engine) as session:
            result = session.execute(
                Market.__table__.update()
                .where(Market.market_id.in_(batch))
                .values(status=ARCHIVED_STATUS)
            )
            session.commit()
            archived += result.rowcount or 0

    reclaimed = None
    if vacuum and engine.dialect.name == "postgresql" and deleted:
        # VACUUM cannot run inside a transaction block.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            logger.info("VACUUM FULL markets — takes an ACCESS EXCLUSIVE lock")
            conn.execute(text("VACUUM (FULL, ANALYZE) markets"))
        after = _database_size(engine)
        if plan.size_before is not None and after is not None:
            reclaimed = plan.size_before - after

    logger.info(
        "Purge executed: %d deleted, %d archived, %s reclaimed",
        deleted, archived, _mb(reclaimed) if reclaimed is not None else "unknown",
    )
    return {
        "deleted": deleted,
        "archived": archived,
        "exempt": plan.exempt,
        "size_after": _database_size(engine),
        "reclaimed_bytes": reclaimed,
    }


def _mb(value: Optional[int]) -> str:
    return "?" if value is None else f"{value / 1_048_576:.1f} MB"


def format_plan(plan: PurgePlan, executed: Optional[Dict] = None) -> str:
    lines = [
        "MARKET PURGE " + ("EXECUTED" if executed else "DRY RUN"),
        "",
        f"  database size now  : {_mb(plan.size_before)}",
        f"  avg markets row    : "
        f"{plan.avg_market_row_bytes:.0f} bytes" if plan.avg_market_row_bytes
        else "  avg markets row    : n/a (not postgres)",
        "",
        f"  DELETABLE (no dependent rows at all) : {plan.deletable:,}",
        f"  ARCHIVABLE (row kept, status corrected): {plan.archivable:,}",
        f"  EXEMPT (open position / being recorded): {plan.exempt:,}",
        "",
    ]

    if plan.estimated_reclaim_bytes:
        lines.append(
            f"  estimated reclaim  : {_mb(plan.estimated_reclaim_bytes)} "
            f"(measured avg row width x deletable rows, before VACUUM FULL)"
        )
        lines.append("")

    if plan.deletable_by_series:
        lines.append("  deletable by series:")
        for series, count in plan.deletable_by_series[:15]:
            lines.append(f"    {series:34s} {count:>10,}")
        lines.append("")

    if plan.archivable_by_series:
        lines.append("  archivable by series:")
        for series, count in plan.archivable_by_series[:15]:
            lines.append(f"    {series:34s} {count:>10,}")
        lines.append("")

    if executed:
        lines.append(
            f"  RESULT: {executed['deleted']:,} deleted, "
            f"{executed['archived']:,} archived, "
            f"{_mb(executed.get('reclaimed_bytes'))} reclaimed, "
            f"now {_mb(executed.get('size_after'))}"
        )
    else:
        lines.append(
            f"  Nothing was changed. Re-run with --confirm {CONFIRM_TOKEN} to apply."
        )
        lines.append(
            "  NOTE: execution runs VACUUM (FULL, ANALYZE) on markets, which "
            "takes an ACCESS EXCLUSIVE lock. A trading cycle overlapping it "
            "will block or lose one tick."
        )
    return "\n".join(lines)
