"""A read-only census of the database, printed to the Actions run summary.

Production is Neon; its URL is an Actions secret, so no laptop can query it.
When the DB hit 68% of its cap and the funnel reported 328,099 open markets
against 24 actually scored, every explanation was equally consistent with the
code and none could be tested. This makes the question answerable from the
Actions page, now and the next time.

Deliberately SELECT-only. It is reachable with no confirmation token precisely
because it cannot change anything — a diagnostic that needs a token is a
diagnostic nobody runs while the thing is on fire.

Two numbers matter more than the rest and are reported side by side:

  open markets      — the funnel's own denominator, imported rather than
                      re-derived, so the report and the funnel cannot disagree
  fresh snapshots   — markets the scorer can actually reach, i.e. those whose
                      latest snapshot is inside MAX_SNAPSHOT_AGE_MINUTES

The gap between them is the whole diagnosis. A large open count made entirely
of long-dated rows nothing will ever trade is a storage problem; a large open
count of near-dated rows is a scoring problem. The close-date horizon buckets
tell those apart.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Engine, and_, func, or_, select, text

from src.database import get_session
from src.maintenance.expire_markets import OPEN_STATUSES, open_market_count
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.recorder.health import format_recorder_health, recorder_health
from src.trading_config import MAX_DAYS_TO_EXPIRY, MAX_SNAPSHOT_AGE_MINUTES

logger = logging.getLogger(__name__)

# Buckets chosen around the velocity limit: anything past MAX_DAYS_TO_EXPIRY is
# refused by the filter regardless, so rows out there are storage and nothing
# else.
_HORIZON_BUCKETS: Tuple[Tuple[str, Optional[int]], ...] = (
    ("<=7d", 7),
    ("8-30d", 30),
    ("31-90d", 90),
    (">90d", None),
)

_GROWTH_DAYS = 21


@dataclass
class DbStats:
    now: dt.datetime
    dialect: str = ""
    total_bytes: Optional[int] = None
    table_bytes: List[Tuple[str, int, Optional[int]]] = field(default_factory=list)
    row_counts: Dict[str, int] = field(default_factory=dict)
    status_counts: Dict[str, int] = field(default_factory=dict)
    open_markets: int = 0
    open_status_past_close: int = 0
    horizon: Dict[str, int] = field(default_factory=dict)
    top_open_prefixes: List[Tuple[str, int]] = field(default_factory=list)
    markets_created_per_day: List[Tuple[dt.date, int]] = field(default_factory=list)
    markets_with_fresh_snapshot: int = 0
    snapshot_span: Tuple[Optional[dt.datetime], Optional[dt.datetime]] = (None, None)
    snapshots_per_day: List[Tuple[dt.date, int]] = field(default_factory=list)
    # Recorder coverage split into live / dead / unattributable. Carried here
    # because "how full is the DB" and "how much of the record was real" are
    # the same dispatch against a database only Actions can reach.
    recorder: Dict[str, object] = field(default_factory=dict)


def _table_names(engine: Engine) -> List[str]:
    from sqlalchemy import inspect

    return sorted(inspect(engine).get_table_names())


def _postgres_sizes(session) -> List[Tuple[str, int, Optional[int]]]:
    rows = session.execute(
        text(
            """
            SELECT c.relname,
                   pg_total_relation_size(c.oid) AS total,
                   s.n_live_tup
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY pg_total_relation_size(c.oid) DESC
            """
        )
    ).all()
    return [(r[0], int(r[1] or 0), r[2]) for r in rows]


def _day(value) -> Optional[dt.date]:
    """SQLite hands back strings where Postgres hands back datetimes."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def collect(engine: Engine, now: Optional[dt.datetime] = None) -> DbStats:
    """Census the database. Runs SELECTs only."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stats = DbStats(now=now, dialect=engine.dialect.name)

    # The funnel's denominator, not a copy of it.
    stats.open_markets = open_market_count(engine, now=now)

    fresh_cutoff = now - dt.timedelta(minutes=MAX_SNAPSHOT_AGE_MINUTES)
    growth_cutoff = now - dt.timedelta(days=_GROWTH_DAYS)

    with get_session(engine) as session:
        if stats.dialect == "postgresql":
            stats.total_bytes = session.execute(
                text("SELECT pg_database_size(current_database())")
            ).scalar_one()
            stats.table_bytes = _postgres_sizes(session)

        for table in _table_names(engine):
            try:
                stats.row_counts[table] = session.execute(
                    select(func.count()).select_from(text(table))
                ).scalar_one()
            except Exception:
                # A table the ORM does not know about is still worth listing;
                # a failure to count one must not lose the other twenty.
                logger.warning("Could not count %s", table, exc_info=True)

        stats.status_counts = {
            status: count
            for status, count in session.execute(
                select(Market.status, func.count()).group_by(Market.status)
            ).all()
        }

        stats.open_status_past_close = session.execute(
            select(func.count()).select_from(Market).where(
                and_(
                    Market.status.in_(OPEN_STATUSES),
                    Market.close_date.isnot(None),
                    Market.close_date < now,
                )
            )
        ).scalar_one()

        open_clause = and_(
            Market.status.in_(OPEN_STATUSES),
            or_(Market.close_date.is_(None), Market.close_date >= now),
        )

        previous = 0
        for label, days in _HORIZON_BUCKETS:
            if days is None:
                count = stats.open_markets - previous
            else:
                count = session.execute(
                    select(func.count()).select_from(Market).where(
                        open_clause,
                        Market.close_date < now + dt.timedelta(days=days),
                    )
                ).scalar_one()
                # Buckets are cumulative queries turned into disjoint counts,
                # so they sum to the open total by construction rather than by
                # hoping four independent ranges tile it.
                count, previous = count - previous, count
            stats.horizon[label] = count

        stats.top_open_prefixes = [
            (prefix, count)
            for prefix, count in session.execute(
                select(
                    func.substr(
                        Market.market_id, 1, func.instr(Market.market_id, "-") - 1
                    ).label("prefix")
                    if stats.dialect == "sqlite"
                    else func.split_part(Market.market_id, "-", 1).label("prefix"),
                    func.count(),
                )
                .where(open_clause)
                .group_by(text("prefix"))
                .order_by(func.count().desc())
                .limit(15)
            ).all()
        ]

        stats.markets_created_per_day = [
            (_day(day), count)
            for day, count in session.execute(
                select(func.date(Market.created_at).label("d"), func.count())
                .where(Market.created_at >= growth_cutoff)
                .group_by(text("d"))
                .order_by(text("d"))
            ).all()
        ]

        stats.markets_with_fresh_snapshot = session.execute(
            select(func.count(func.distinct(PriceSnapshot.market_id))).where(
                PriceSnapshot.timestamp >= fresh_cutoff
            )
        ).scalar_one()

        stats.snapshot_span = session.execute(
            select(func.min(PriceSnapshot.timestamp), func.max(PriceSnapshot.timestamp))
        ).one()

        stats.snapshots_per_day = [
            (_day(day), count)
            for day, count in session.execute(
                select(func.date(PriceSnapshot.timestamp).label("d"), func.count())
                .where(PriceSnapshot.timestamp >= growth_cutoff)
                .group_by(text("d"))
                .order_by(text("d"))
            ).all()
        ]

    stats.recorder = recorder_health(engine, now=now)
    return stats


def _mb(value: Optional[int]) -> str:
    return "?" if value is None else f"{value / 1_048_576:.1f} MB"


def format_report(stats: DbStats) -> str:
    lines: List[str] = [
        f"DB CENSUS ({stats.dialect}) at {stats.now:%Y-%m-%d %H:%M}Z",
        "",
        f"  total size            : {_mb(stats.total_bytes)}",
        f"  open markets (funnel) : {stats.open_markets}",
        f"  open status past close: {stats.open_status_past_close}"
        "   <- expire_closed_markets has not swept these",
        f"  scorer can reach      : {stats.markets_with_fresh_snapshot}"
        f"   (snapshot < {MAX_SNAPSHOT_AGE_MINUTES}min)",
        "",
    ]

    if stats.table_bytes:
        lines.append("  bytes by table:")
        for name, total, live in stats.table_bytes[:15]:
            lines.append(f"    {name:28s} {_mb(total):>10s}  ~{live if live is not None else '?'} rows")
        lines.append("")

    lines.append("  rows by table:")
    for name, count in sorted(stats.row_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {name:28s} {count:>12,}")
    lines.append("")

    lines.append("  markets by status:")
    for status, count in sorted(stats.status_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {status:28s} {count:>12,}")
    lines.append("")

    lines.append(
        f"  open markets by close horizon (velocity limit is "
        f"{MAX_DAYS_TO_EXPIRY}d — beyond it nothing can trade):"
    )
    for label, _ in _HORIZON_BUCKETS:
        lines.append(f"    {label:28s} {stats.horizon.get(label, 0):>12,}")
    lines.append("")

    if stats.top_open_prefixes:
        lines.append("  open markets by series prefix:")
        for prefix, count in stats.top_open_prefixes:
            lines.append(f"    {prefix:28s} {count:>12,}")
        lines.append("")

    if stats.markets_created_per_day:
        lines.append(f"  market rows first seen per day (last {_GROWTH_DAYS}d):")
        for day, count in stats.markets_created_per_day:
            lines.append(f"    {str(day):28s} {count:>12,}")
        lines.append("")

    if stats.recorder:
        lines.append("  recorder:")
        lines.append("    " + format_recorder_health(stats.recorder).replace("\n", "\n  "))
        lines.append("")

    first, last = stats.snapshot_span
    lines.append(f"  price_snapshots span  : {first} -> {last}")
    if stats.snapshots_per_day:
        lines.append(f"  price_snapshots per day (last {_GROWTH_DAYS}d):")
        for day, count in stats.snapshots_per_day:
            lines.append(f"    {str(day):28s} {count:>12,}")

    return "\n".join(lines)
