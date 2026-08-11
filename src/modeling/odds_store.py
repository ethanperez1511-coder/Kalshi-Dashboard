"""Cross-process state for odds fetching: the cache and the quota ledger.

Both used to be module globals, which silently stopped working when the bot
moved to a per-cycle cron process. Anything that must survive a cycle lives
here, in the database.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Engine

from src.database import get_session
from src.models.odds import OddsCacheEntry, OddsQuotaUsage

logger = logging.getLogger(__name__)


def month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat stored times as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class OddsCacheStore:
    """TTL cache for raw provider payloads, keyed by sport."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self, sport_key: str) -> Optional[Tuple[datetime, Any, str]]:
        """Return (fetched_at, payload, source) for a sport, or None."""
        with get_session(self._engine) as session:
            entry = (
                session.query(OddsCacheEntry)
                .filter_by(sport_key=sport_key)
                .order_by(OddsCacheEntry.fetched_at.desc())
                .first()
            )
            if entry is None:
                return None
            return _aware(entry.fetched_at), json.loads(entry.payload), entry.source

    def is_fresh(self, sport_key: str, ttl_seconds: float, now: datetime) -> bool:
        cached = self.get(sport_key)
        if cached is None:
            return False
        fetched_at, _, _ = cached
        return (now - fetched_at).total_seconds() < ttl_seconds

    def put(self, sport_key: str, source: str, payload: Any, now: datetime) -> None:
        with get_session(self._engine) as session:
            entry = (
                session.query(OddsCacheEntry)
                .filter_by(sport_key=sport_key, source=source)
                .first()
            )
            if entry is None:
                entry = OddsCacheEntry(sport_key=sport_key, source=source)
                session.add(entry)
            entry.payload = json.dumps(payload)
            entry.fetched_at = now
            session.commit()


class QuotaLedger:
    """Per-month request accounting for a metered provider."""

    def __init__(self, engine: Engine, cap: int):
        self._engine = engine
        self.cap = cap

    def used(self, month: str, source: str) -> int:
        with get_session(self._engine) as session:
            row = session.query(OddsQuotaUsage).filter_by(month=month, source=source).first()
            return row.requests_used if row else 0

    def remaining(self, month: str, source: str) -> int:
        return max(0, self.cap - self.used(month, source))

    def charge(self, month: str, source: str, count: int = 1) -> int:
        """Record spend BEFORE the request. Over-counting on a crash is the
        safe direction — the penalty for overrunning the cap is a month of
        blindness, the penalty for over-counting is one skipped refresh."""
        with get_session(self._engine) as session:
            row = session.query(OddsQuotaUsage).filter_by(month=month, source=source).first()
            if row is None:
                row = OddsQuotaUsage(month=month, source=source, requests_used=0)
                session.add(row)
            row.requests_used = (row.requests_used or 0) + count
            session.commit()
            return row.requests_used


def default_ttl_seconds(n_sports: int, monthly_cap: int) -> float:
    """Longest refresh interval that keeps a month of polling inside the cap.

    A 30-day month is 720 hours; each sport gets cap/n_sports requests, so one
    refresh every 720 * n_sports / cap hours spends the budget exactly. With 3
    sports and a 500 cap that is ~4.3h.
    """
    if n_sports <= 0 or monthly_cap <= 0:
        return 3600.0
    return 720.0 * 3600.0 * n_sports / monthly_cap


def quota_snapshot(
    engine: Engine,
    cap: int,
    now: Optional[datetime] = None,
    source: str = "the_odds_api",
) -> Dict[str, Any]:
    """Quota position plus a burn-rate projection for the dashboard."""
    now = now or datetime.now(timezone.utc)
    month = month_key(now)
    ledger = QuotaLedger(engine, cap=cap)
    used = ledger.used(month, source)

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed_days = (
        now.day - 1
        + (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0
    )
    elapsed_days = max(elapsed_days, 1e-9)

    burn = used / elapsed_days if used else 0.0
    projected = burn * days_in_month
    remaining = max(0, cap - used)
    days_to_exhaustion = (remaining / burn) if burn > 0 else None

    return {
        "month": month,
        "source": source,
        "used": used,
        "cap": cap,
        "remaining": remaining,
        "burn_per_day": burn,
        "projected_month_end": projected,
        "days_in_month": days_in_month,
        "days_elapsed": elapsed_days,
        "days_to_exhaustion": days_to_exhaustion,
        "projected_overrun": projected > cap,
    }


def cache_status(engine: Engine, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Per-sport cache age, so the dashboard can show what is going stale."""
    now = now or datetime.now(timezone.utc)
    with get_session(engine) as session:
        entries = session.query(OddsCacheEntry).all()
        return [
            {
                "sport_key": e.sport_key,
                "source": e.source,
                "fetched_at": _aware(e.fetched_at).isoformat(),
                "age_minutes": round((now - _aware(e.fetched_at)).total_seconds() / 60.0, 1),
            }
            for e in entries
        ]
