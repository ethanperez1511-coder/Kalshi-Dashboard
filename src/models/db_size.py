"""One row per day: how big the database was.

Kept as its own table rather than folded into an existing keyed store, because
the question it answers is a time series and everything else in this schema is
current state. Additive and tiny — one row a day is 365 rows a year.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class DbSizeSample(Base):
    __tablename__ = "db_size_samples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sampled_on: Mapped[date] = mapped_column(Date, unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # Live data (file minus reusable space). The cap projection runs on this:
    # the file can grow while live data is flat, and vice versa.
    live_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, default=None)
    # Per-table sizes, JSON {table: {"heap", "index", "toast"}} in bytes, so a
    # day's growth can be attributed from the digest (2026-10-14: +59 MB of
    # live data could not be broken down without a retention log).
    tables_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    sampled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        default=lambda: datetime.now(timezone.utc),
    )
