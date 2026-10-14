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
import json
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Engine, select, text

from src.database import get_session
# One source of truth for the cap and the budget. This file had its own copy of
# the old 512 MiB tier, which kept the digest at "134% of tier" after Neon
# doubled the cap.
from src.maintenance.retention import MB, NEON_CAP_BYTES, size_status
from src.models.db_size import DbSizeSample

logger = logging.getLogger(__name__)


# Enough days to average out one big prune without hiding a fresh firehose.
TREND_DAYS = 7


def database_size_bytes(engine: Engine) -> Optional[int]:
    if engine.dialect.name != "postgresql":
        return None
    with get_session(engine) as session:
        return session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar_one()


def table_sizes(engine: Engine) -> Optional[Dict[str, Dict[str, int]]]:
    """Heap, index and TOAST bytes per public table. Catalog reads only."""
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT relname,
                   pg_relation_size(relid, 'main'),
                   pg_indexes_size(relid),
                   pg_table_size(relid) - pg_relation_size(relid, 'main')
            FROM pg_stat_user_tables WHERE schemaname = 'public'
            """
        )).all()
    return {r[0]: {"heap": int(r[1]), "index": int(r[2]), "toast": int(r[3])} for r in rows}


def record_sample(
    engine: Engine, now: Optional[dt.datetime] = None, size_bytes: Optional[int] = None,
    live_bytes: Optional[int] = None,
    tables: Optional[Dict[str, Dict[str, int]]] = None,
) -> Optional[int]:
    """Record today's size, live data and per-table sizes. One row per day;
    later calls that day update it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    size = size_bytes if size_bytes is not None else database_size_bytes(engine)
    if size is None:
        return None
    if live_bytes is None and size_bytes is None:
        from src.maintenance.retention import live_bytes_estimate

        live_bytes = live_bytes_estimate(engine)
    if tables is None and size_bytes is None:
        tables = table_sizes(engine)
    tables_json = json.dumps(tables, sort_keys=True) if tables else None

    today = now.date()
    with get_session(engine) as session:
        existing = session.execute(
            select(DbSizeSample).where(DbSizeSample.sampled_on == today)
        ).scalar_one_or_none()
        if existing is None:
            session.add(DbSizeSample(
                sampled_on=today, size_bytes=size, live_bytes=live_bytes,
                tables_json=tables_json, sampled_at=now,
            ))
        else:
            existing.size_bytes = size
            existing.live_bytes = live_bytes
            existing.tables_json = tables_json
            existing.sampled_at = now
        session.commit()
    return size


def _table_deltas(before: Optional[str], after: Optional[str]) -> List[Tuple[str, int, int, int]]:
    """(table, heap, index, toast) change between two samples, largest first."""
    if not before or not after:
        return []
    old, new = json.loads(before), json.loads(after)
    out = []
    for name, now_sizes in new.items():
        was = old.get(name, {"heap": 0, "index": 0, "toast": 0})
        out.append((name, now_sizes["heap"] - was["heap"],
                    now_sizes["index"] - was["index"], now_sizes["toast"] - was["toast"]))
    return sorted(out, key=lambda d: -abs(d[1] + d[2] + d[3]))


def growth(engine: Engine, now: Optional[dt.datetime] = None) -> dict:
    """Trailing MB/day and a naive days-to-full, or an explicit "not yet"."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now.date() - dt.timedelta(days=TREND_DAYS)

    with get_session(engine) as session:
        raw = session.execute(
            select(DbSizeSample.sampled_on, DbSizeSample.size_bytes,
                   DbSizeSample.live_bytes, DbSizeSample.tables_json)
            .where(DbSizeSample.sampled_on >= cutoff)
            .order_by(DbSizeSample.sampled_on)
        ).all()
    if not raw:
        return {"samples": 0, "current_bytes": None, "mb_per_day": None,
                "days_to_full": None}

    file_now, live_now = raw[-1][1], raw[-1][2]
    table_deltas = _table_deltas(raw[-2][3], raw[-1][3]) if len(raw) >= 2 else []
    base = {"current_bytes": file_now, "current_live": live_now,
            "table_deltas": table_deltas}

    # Project on LIVE data: the file can grow while live data is flat
    # (reusable space being refilled). Samples from before live_bytes existed
    # (2026-10-13) are skipped rather than forcing the whole window onto the
    # file, which is what printed "NEON CAP IN ~3.8 DAYS" on 2026-10-14.
    live_rows = [(r[0], r[2]) for r in raw if r[2] is not None]
    if live_now is not None:
        rows, basis = live_rows, "live"
    else:
        rows, basis = [(r[0], r[1]) for r in raw], "file"

    first_day, first_size = rows[0]
    last_day, last_size = rows[-1]
    span_days = (last_day - first_day).days
    if span_days <= 0:
        # One day of history is a level, not a rate.
        return {**base, "samples": len(rows), "mb_per_day": None,
                "days_to_full": None, "basis": basis}

    bytes_per_day = (last_size - first_size) / span_days
    headroom = NEON_CAP_BYTES - last_size
    days_to_full = (
        headroom / bytes_per_day if bytes_per_day > 0 and headroom > 0 else None
    )
    return {
        **base,
        "samples": len(rows),
        "mb_per_day": bytes_per_day / MB,
        "days_to_full": days_to_full,
        "span_days": span_days,
        "basis": basis,
    }


def format_growth(data: dict) -> str:
    current = data.get("current_bytes")
    if current is None:
        return "💾 DB: size unavailable (not postgres)"

    # Same headroom as retention prints: cap minus live data.
    line = f"💾 DB: {size_status(current, data.get('current_live'))}"

    deltas = [d for d in data.get("table_deltas") or [] if abs(sum(d[1:])) >= MB]
    if deltas:
        parts = []
        for name, heap, index, toast in deltas[:4]:
            part = f"{name} {(heap + index + toast) / MB:+.0f} MB"
            if index and heap:
                part += f" (heap {heap / MB:+.0f}, index {index / MB:+.0f})"
            parts.append(part)
        line += "\n   since yesterday, by table: " + " · ".join(parts)

    rate = data.get("mb_per_day")
    if rate is None:
        return line + f" — no {data.get('basis', 'file')} trend yet ({data.get('samples', 0)} day(s))"

    line += f", {data.get('basis', 'file')} data {rate:+.1f} MB/day over {data.get('span_days')}d"
    days = data.get("days_to_full")
    if days is not None and days < 30:
        line += (
            f"\n   ⚠️ NEON CAP IN ~{days:.1f} DAYS at this rate — at the cap, "
            f"writes AND deletes are refused"
        )
    elif rate <= 0:
        line += " — shrinking"
    return line
