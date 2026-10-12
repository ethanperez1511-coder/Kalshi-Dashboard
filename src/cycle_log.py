"""Count the cycles that ran, and tell an outside service each time one did.

GitHub's scheduler delivered a fraction of the cycles asked for, and on
2026-10-06 the session's triggers and its watchdog all failed to fire with no
error anywhere. A watchdog on the same scheduler shares that failure (L32).

  record_cycle   one row per cycle; the digest reads it as "cycles in the
                 last 24 h vs expected", so a gap is a number, not a silence
  ping_deadman   GET to DEADMAN_PING_URL after each successful cycle. An
                 outside service alerts when the pings STOP, which is the
                 one failure nothing inside GitHub can report. The URL
                 carries no repository permission of any kind.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, Optional

from sqlalchemy import Engine, delete, func, select

from src.database import get_session
from src.models.cycle_run import CycleRun
from src.session import CYCLE_SECONDS, SESSION_END, SESSION_START

logger = logging.getLogger(__name__)

SESSION_SOURCE = "market-session"
_window = (dt.datetime.combine(dt.date.today(), SESSION_END)
           - dt.datetime.combine(dt.date.today(), SESSION_START))
# Cycles a full session runs: 15:00, 15:15, ... 20:45.
SESSION_EXPECTED = int(_window.total_seconds() // CYCLE_SECONDS)
KEEP_DAYS = 60


def record_cycle(engine: Engine, started: dt.datetime, finished: dt.datetime,
                 ok: bool, source: str, equity: Optional[float] = None) -> None:
    with get_session(engine) as session:
        session.add(CycleRun(started_at=started, finished_at=finished, ok=ok,
                             source=(source or "manual")[:60], equity=equity))
        session.execute(delete(CycleRun).where(
            CycleRun.started_at < started - dt.timedelta(days=KEEP_DAYS)
        ))
        session.commit()


def ping_deadman(url: str) -> bool:
    """Best effort. A ping that fails must never fail the cycle it reports."""
    if not url:
        return False
    try:
        import httpx

        httpx.get(url, timeout=5)
        return True
    except Exception:
        logger.warning("Dead-man ping failed (non-fatal)", exc_info=True)
        return False


def cycles_digest(engine: Engine, now: Optional[dt.datetime] = None) -> Dict[str, int]:
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=24)
    with get_session(engine) as session:
        rows = session.execute(
            select(CycleRun.source, CycleRun.ok, func.count())
            .where(CycleRun.started_at >= since)
            .group_by(CycleRun.source, CycleRun.ok)
        ).all()
    out = {"session": 0, "backstop": 0, "failed": 0, "missed": []}
    with get_session(engine) as session:
        starts = [
            t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
            for (t,) in session.execute(
                select(CycleRun.started_at)
                .where(CycleRun.started_at >= since, CycleRun.source == SESSION_SOURCE,
                       CycleRun.ok.is_(True))
            ).all()
        ]
    out["missed"] = _missed_slots(starts, now)
    for source, ok, count in rows:
        if not ok:
            out["failed"] += count
        elif source == SESSION_SOURCE:
            out["session"] += count
        else:
            out["backstop"] += count
    return out


def _missed_slots(starts, now: dt.datetime):
    """Slots of the most recent COMPLETED session window with no session cycle
    starting inside them. A cycle starts within a minute or two of its slot."""
    close_today = dt.datetime.combine(now.date(), SESSION_END, tzinfo=dt.timezone.utc)
    close = close_today if now >= close_today else close_today - dt.timedelta(days=1)
    open_ = dt.datetime.combine(close.date(), SESSION_START, tzinfo=dt.timezone.utc)
    missed = []
    slot = open_
    while slot < close:
        if not any(slot <= t < slot + dt.timedelta(seconds=CYCLE_SECONDS) for t in starts):
            missed.append(slot.strftime("%H:%M"))
        slot += dt.timedelta(seconds=CYCLE_SECONDS)
    return missed


def format_cycles_digest(data: Dict[str, int]) -> str:
    gap = data["session"] < SESSION_EXPECTED
    line = (
        f"{'⚠️' if gap else '🔁'} Cycles 24h: session {data['session']}/{SESSION_EXPECTED} "
        f"expected, backstop {data['backstop']}"
    )
    if data["failed"]:
        line += f", {data['failed']} failed"
    if gap and data.get("missed"):
        line += " — missed " + ", ".join(data["missed"])
    elif gap:
        line += " — the session missed cycles; check market-session runs"
    return line
