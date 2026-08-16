"""Database size, sampled daily, reported as a trend.

The parlay firehose was found at 68% of the tier with about two days of
headroom left, and the only reason it was found at all is that someone went
looking. Nothing reported the size, so nothing could report that it was
climbing — the first observation and the emergency were the same event.

A level is not a warning. 376 MB is fine on a database that has been 370 MB for
a month and is an emergency on one that was 200 MB on Friday. So this records
one sample per day and reports MB/day across the window, which is the number
that would have shown the parlay mint as a trend line days before it became a
deadline.

Days to full is computed from the trailing rate and printed alongside. It is
deliberately naive — a straight-line extrapolation of recent growth — because
the alternative to a rough number here is the no number that let this happen.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import List, Optional, Tuple

from sqlalchemy import Engine, select, text

from src.database import get_session
from src.models.db_size import DbSizeSample

logger = logging.getLogger(__name__)

TIER_LIMIT_BYTES = 512 * 1024 * 1024

# Enough days to average out one big prune without hiding a fresh firehose.
TREND_DAYS = 7


def database_size_bytes(engine: Engine) -> Optional[int]:
    if engine.dialect.name != "postgresql":
        return None
    with get_session(engine) as session:
        return session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar_one()


def record_sample(
    engine: Engine, now: Optional[dt.datetime] = None, size_bytes: Optional[int] = None,
) -> Optional[int]:
    """Record today's size. One row per day; later calls that day update it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    size = size_bytes if size_bytes is not None else database_size_bytes(engine)
    if size is None:
        return None

    today = now.date()
    with get_session(engine) as session:
        existing = session.execute(
            select(DbSizeSample).where(DbSizeSample.sampled_on == today)
        ).scalar_one_or_none()
        if existing is None:
            session.add(DbSizeSample(
                sampled_on=today, size_bytes=size, sampled_at=now,
            ))
        else:
            existing.size_bytes = size
            existing.sampled_at = now
        session.commit()
    return size


def growth(engine: Engine, now: Optional[dt.datetime] = None) -> dict:
    """Trailing MB/day and a naive days-to-full, or an explicit "not yet"."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now.date() - dt.timedelta(days=TREND_DAYS)

    with get_session(engine) as session:
        rows: List[Tuple[dt.date, int]] = session.execute(
            select(DbSizeSample.sampled_on, DbSizeSample.size_bytes)
            .where(DbSizeSample.sampled_on >= cutoff)
            .order_by(DbSizeSample.sampled_on)
        ).all()

    if not rows:
        return {"samples": 0, "current_bytes": None, "mb_per_day": None,
                "days_to_full": None}

    first_day, first_size = rows[0]
    last_day, last_size = rows[-1]
    span_days = (last_day - first_day).days

    if span_days <= 0:
        # One day of history is a level, not a rate. Saying so beats printing
        # a slope computed from a single point.
        return {"samples": len(rows), "current_bytes": last_size,
                "mb_per_day": None, "days_to_full": None}

    bytes_per_day = (last_size - first_size) / span_days
    headroom = TIER_LIMIT_BYTES - last_size
    days_to_full = (
        headroom / bytes_per_day if bytes_per_day > 0 and headroom > 0 else None
    )

    return {
        "samples": len(rows),
        "current_bytes": last_size,
        "mb_per_day": bytes_per_day / 1_048_576,
        "days_to_full": days_to_full,
        "span_days": span_days,
    }


def format_growth(data: dict) -> str:
    current = data.get("current_bytes")
    if current is None:
        return "💾 DB: size unavailable (not postgres)"

    used_mb = current / 1_048_576
    pct = 100.0 * current / TIER_LIMIT_BYTES
    line = f"💾 DB: {used_mb:.0f} MB ({pct:.0f}% of tier)"

    rate = data.get("mb_per_day")
    if rate is None:
        return line + f" — no trend yet ({data.get('samples', 0)} sample(s))"

    line += f", {rate:+.1f} MB/day over {data.get('span_days')}d"
    days = data.get("days_to_full")
    if days is not None and days < 30:
        line += f"\n   ⚠️ FULL IN ~{days:.1f} DAYS at this rate"
    elif rate <= 0:
        line += " — shrinking"
    return line
