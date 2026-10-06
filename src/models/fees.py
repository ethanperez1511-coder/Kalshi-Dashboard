"""The fee schedule Kalshi last published for each series we trade.

One row per series. `observed_at` is the last SUCCESSFUL fetch, the clock the
24-hour fallback runs on; `attempted_at` is the last try, so a series whose
most recent fetch failed is visible as one running on a cached fee.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class SeriesFee(Base):
    __tablename__ = "series_fees"

    series: Mapped[str] = mapped_column(String(64), primary_key=True)
    fee_type: Mapped[str] = mapped_column(String(64))
    fee_multiplier: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
