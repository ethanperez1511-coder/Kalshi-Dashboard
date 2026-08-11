from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Float, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

# Whether this market's threshold terms were successfully read. Recorded
# explicitly rather than inferred from nulls at read time, so "we could not
# parse this contract" is a visible, countable state instead of a silent one.
TERMS_PARSED = "parsed"
TERMS_UNPARSED = "unparsed"       # a threshold contract we could NOT read
TERMS_UNSUPPORTED = "unsupported"  # readable, but a type we do not model yet
TERMS_NOT_APPLICABLE = "n/a"      # not a threshold contract at all


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(100), index=True)
    sub_category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default=None)
    close_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), index=True)
    rules: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    series_ticker: Mapped[Optional[str]] = mapped_column(
        String(60), nullable=True, default=None, index=True
    )
    # Threshold terms, parsed at ingest. `strike_direction` is "above" or
    # "below" and is ALWAYS strict: ">90°" settles NO at exactly 90. The same
    # city lists both directions on the same day, so this can never be assumed
    # from the series or the city.
    strike_direction: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, default=None
    )
    strike_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    strike_unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default=None)
    terms_status: Mapped[str] = mapped_column(
        String(12), default=TERMS_NOT_APPLICABLE, server_default=TERMS_NOT_APPLICABLE
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
