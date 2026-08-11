"""Persistence for odds data and odds-provider quota accounting.

Both tables exist for one reason: the trading pipeline runs as a GitHub Actions
cron, so every cycle is a fresh process. Anything held in module memory —
including the odds cache and any "requests spent so far" counter — is gone by
the next tick. State that must outlive a cycle has to live in the database.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class OddsCacheEntry(Base):
    """One cached odds fetch for a (sport, source) pair.

    `payload` is the provider's raw JSON response, stored verbatim so a parser
    change can be re-run against it without spending quota again.
    """

    __tablename__ = "odds_cache"
    __table_args__ = (UniqueConstraint("sport_key", "source", name="uq_odds_cache_sport_source"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sport_key: Mapped[str] = mapped_column(String(50), index=True)
    source: Mapped[str] = mapped_column(String(30))
    payload: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class OddsQuotaUsage(Base):
    """Requests spent against a metered odds provider, per calendar month.

    Charged before the request goes out, so a crash mid-request costs us an
    over-count rather than an under-count — the safe direction when the penalty
    for overrunning is a month of blindness.
    """

    __tablename__ = "odds_quota"
    __table_args__ = (UniqueConstraint("month", "source", name="uq_odds_quota_month_source"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "2026-08"
    source: Mapped[str] = mapped_column(String(30))
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
