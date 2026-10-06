"""Measure the bytes this process moves to and from the database.

Neon closed every connection on 2026-08-23 for exceeding a monthly DATA
TRANSFER quota. Nothing in this system had ever measured transfer: the tests
bounded query COUNT, the digest tracked STORAGE, and the one query that
actually did the damage — an unbounded projection over the whole recorded tape,
run once per cycle — was invisible on both axes.

This is our own estimate, not Neon's accounting, and it is deliberately
approximate. Its job is to make the trend visible between billing periods, so
a new consumer shows up as a rising line rather than as an outage. Exact
reconciliation belongs to the provider's console; a number that is roughly
right and always present beats an exact one nobody sees.

Counted per statement:
  out  bytes returned to us — the result rows. This is what an egress quota
       bills, and it is what the fatal query produced 39 MB of, 288 times a day.
  in   the statement text plus its bound parameters, which is what a bulk
       insert of 2,500 markets actually costs.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Optional

from sqlalchemy import Engine, event, func, select

from src.database import get_session
from src.models.transfer import TransferSample

logger = logging.getLogger(__name__)

# Neon's free plan. Overridable because the plan is the operator's decision and
# this must not silently keep reporting against a limit that has changed.
from src.trading_config import (
    COMPUTE_QUOTA_HOURS,
    CYCLE_COMPUTE_MINUTES,
    CYCLE_MINUTES,
    NEON_MIN_CU,
    TRANSFER_QUOTA_GB,
    TRANSFER_WARN_FRACTION,
)

QUOTA_BYTES = int(TRANSFER_QUOTA_GB * 1_000_000_000)

_LOCK = threading.Lock()
_STATE = {"in": 0, "out": 0, "statements": 0}


def reset() -> None:
    with _LOCK:
        _STATE.update({"in": 0, "out": 0, "statements": 0})


def totals() -> dict:
    with _LOCK:
        return dict(_STATE)


def _row_bytes(row) -> int:
    total = 0
    for value in row:
        if value is None:
            continue
        if isinstance(value, (bytes, str)):
            total += len(value)
        else:
            total += 8
    return total


def attach(engine: Engine) -> None:
    """Start counting on this engine. Idempotent."""
    if getattr(engine, "_transfer_metered", False):
        return
    engine._transfer_metered = True

    @event.listens_for(engine, "after_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        try:
            size = len(statement)
            if parameters:
                size += len(repr(parameters))
            with _LOCK:
                _STATE["in"] += size
                _STATE["statements"] += 1
        except Exception:  # pragma: no cover - metering must never raise
            logger.debug("transfer meter failed on a statement", exc_info=True)

    @event.listens_for(engine, "handle_error")
    def _ignore(context):  # pragma: no cover - passthrough
        return None


def record_result_rows(rows) -> None:
    """Charge the bytes of a result set that crossed the wire."""
    try:
        total = sum(_row_bytes(r) for r in rows)
        with _LOCK:
            _STATE["out"] += total
    except Exception:  # pragma: no cover
        logger.debug("transfer meter failed on a result set", exc_info=True)


def flush(engine: Engine, now: Optional[dt.datetime] = None) -> dict:
    """Fold this process's counts into today's row. Returns the day's totals."""
    now = now or dt.datetime.now(dt.timezone.utc)
    counts = totals()
    today = now.date()

    with get_session(engine) as session:
        row = session.execute(
            select(TransferSample).where(TransferSample.sampled_on == today)
        ).scalar_one_or_none()
        if row is None:
            row = TransferSample(
                sampled_on=today, bytes_in=0, bytes_out=0, statements=0,
            )
            session.add(row)
        row.bytes_in = (row.bytes_in or 0) + counts["in"]
        row.bytes_out = (row.bytes_out or 0) + counts["out"]
        row.statements = (row.statements or 0) + counts["statements"]
        row.updated_at = now
        session.commit()
        result = {
            "bytes_in": row.bytes_in,
            "bytes_out": row.bytes_out,
            "statements": row.statements,
        }
    reset()
    return result


def month_to_date(engine: Engine, now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    first = now.date().replace(day=1)
    with get_session(engine) as session:
        rows = session.execute(
            select(TransferSample.bytes_in, TransferSample.bytes_out)
            .where(TransferSample.sampled_on >= first)
        ).all()

    total_in = sum(r[0] or 0 for r in rows)
    total_out = sum(r[1] or 0 for r in rows)
    days = max(1, len(rows))
    return {
        "days": len(rows),
        "bytes_in": total_in,
        "bytes_out": total_out,
        "total": total_in + total_out,
        "per_day": (total_in + total_out) / days,
        "fraction": (total_in + total_out) / QUOTA_BYTES if QUOTA_BYTES else 0.0,
    }


def format_transfer(data: dict) -> str:
    if not data.get("days"):
        return "📡 Transfer: no samples yet this month"

    total_gb = data["total"] / 1e9
    per_day_mb = data["per_day"] / 1e6
    pct = 100.0 * data["fraction"]
    line = (
        f"📡 Transfer: {total_gb:.2f} GB month-to-date ({pct:.0f}% of "
        f"{TRANSFER_QUOTA_GB:.0f} GB), {per_day_mb:.0f} MB/day over "
        f"{data['days']}d"
    )

    if data["fraction"] >= TRANSFER_WARN_FRACTION:
        remaining = max(0.0, QUOTA_BYTES - data["total"])
        days_left = remaining / data["per_day"] if data["per_day"] else 0
        line += (
            f"\n   ⚠️ OVER {TRANSFER_WARN_FRACTION:.0%} — about {days_left:.1f} "
            f"days of headroom. At 100% Neon closes every connection and the "
            f"whole system stops."
        )
    return line


# --------------------------------------------------------------------------
# Compute hours — the OTHER metered axis, and the one cadence barely moves.
# --------------------------------------------------------------------------

def compute_estimate(engine: Engine, now: Optional[dt.datetime] = None) -> dict:
    """Hours the database compute was most likely awake this month.

    Neon meters compute time as well as transfer and storage, and it scales to
    zero only while nothing is connected. The book recorder holds a connection
    for ~55 minutes of every hour, so it keeps the compute awake roughly
    round-the-clock on its own — which makes it the dominant consumer of this
    axis by a wide margin, and means the 5-to-15-minute cadence change that
    fixed transfer barely touches it.

    Estimated from OUR schedule rather than measured, because the number Neon
    bills is not reachable from inside the job. Distinct recorded hours are the
    honest proxy for "the recorder had a connection open", and they are already
    counted for the day-7 clock.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    first = now.date().replace(day=1)

    from src.models.orderbook_raw import OrderbookDeltaRaw
    from src.recorder.health import _hour_bucket

    with get_session(engine) as session:
        recorder_hours = session.execute(
            select(func.count(func.distinct(
                _hour_bucket(engine, OrderbookDeltaRaw.received_at)
            ))).where(OrderbookDeltaRaw.received_at >= first)
        ).scalar() or 0

        days = session.execute(
            select(func.count(TransferSample.id))
            .where(TransferSample.sampled_on >= first)
        ).scalar() or 0

    # Cycles add compute only when the recorder is not already holding the
    # connection open, which on current schedules is most of the time.
    cycle_hours = days * (1440 / CYCLE_MINUTES) * CYCLE_COMPUTE_MINUTES / 60.0
    awake = max(recorder_hours, 0) + cycle_hours

    # Both awake terms over-count: an hour bucket counts a 55-minute run that
    # straddles :00 twice, and cycles are assumed at the nominal cadence when
    # GitHub delivers far fewer. Multiplied by the floor CU, this lands ABOVE
    # the bill (13.4 vs the console's 7.69 on 2026-10-06), which is the safe
    # side for a number whose job is to show a deadline coming.
    cu_hours = awake * NEON_MIN_CU

    return {
        "recorder_hours": recorder_hours,
        "cycle_hours": round(cycle_hours, 1),
        "awake_hours": round(awake, 1),
        "cu_hours": round(cu_hours, 1),
        "quota_hours": COMPUTE_QUOTA_HOURS,
        "fraction": cu_hours / COMPUTE_QUOTA_HOURS if COMPUTE_QUOTA_HOURS else 0.0,
    }


def format_compute(data: dict) -> str:
    pct = 100.0 * data["fraction"]
    line = (
        f"🖥 Compute: ≤~{data['cu_hours']:.0f} CU-h month-to-date est. "
        f"({pct:.0f}% of {data['quota_hours']:.0f} CU-h) — awake "
        f"{data['awake_hours']:.0f} h x {NEON_MIN_CU} CU floor; "
        f"Neon console is the meter"
    )
    if data["fraction"] >= TRANSFER_WARN_FRACTION:
        line += (
            "\n   ⚠️ Check the console before acting: this estimate runs "
            "high. At 100% Neon suspends compute until the next period."
        )
    return line
