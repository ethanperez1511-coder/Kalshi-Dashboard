"""Track digest sections that keep degrading.

Graceful degradation on day one is resilience. On day four it is a standing
failure wearing a green checkmark: the heartbeat still arrives, still says
"alive", and the section it stopped reporting has been broken all week with
nobody looking.

So a section that reports "unavailable" on N consecutive DAYS raises its own
alert. Days, not cycles — the pipeline runs every five minutes, and counting
cycles would fire within the hour on a transient blip, which is exactly the
resilience this is not supposed to undo.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import List, Optional, Tuple

from sqlalchemy import Engine

from src.database import get_session
from src.models.weather import GuardState

logger = logging.getLogger(__name__)

# Consecutive DAYS of degradation before escalating.
DEGRADED_DAYS_BEFORE_ALERT = 3
_SCOPE_PREFIX = "digest_section:"


def record_section(
    engine: Engine, section: str, healthy: bool, now: Optional[dt.datetime] = None,
) -> Tuple[bool, int]:
    """Record one section's state for today. Returns (should_alert, days_degraded).

    Reuses GuardState as a small keyed state store: `transitions` counts
    consecutive degraded days, `changed_at` is the day last counted. Counting
    once per calendar day is what keeps a five-minute pipeline from escalating
    a blip within the hour.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    scope = f"{_SCOPE_PREFIX}{section}"

    with get_session(engine) as session:
        row = session.query(GuardState).filter_by(scope=scope).first()
        if row is None:
            row = GuardState(scope=scope, paused=False, transitions=0)
            session.add(row)
            session.flush()

        if healthy:
            was = row.transitions or 0
            row.paused = False
            row.transitions = 0
            row.changed_at = now
            session.commit()
            if was >= DEGRADED_DAYS_BEFORE_ALERT:
                return True, 0        # recovery is worth saying out loud too
            return False, 0

        last = row.changed_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        already_counted_today = last is not None and last.date() == now.date()

        if not already_counted_today:
            row.transitions = (row.transitions or 0) + 1
            row.changed_at = now

        days = row.transitions or 0
        should_alert = days == DEGRADED_DAYS_BEFORE_ALERT and not already_counted_today
        row.paused = days >= DEGRADED_DAYS_BEFORE_ALERT
        session.commit()

    return should_alert, days


def degraded_sections(engine: Engine) -> List[Tuple[str, int]]:
    with get_session(engine) as session:
        rows = session.query(GuardState).filter(
            GuardState.scope.like(f"{_SCOPE_PREFIX}%")
        ).all()
        return [
            (r.scope[len(_SCOPE_PREFIX):], r.transitions or 0)
            for r in rows if (r.transitions or 0) > 0
        ]
