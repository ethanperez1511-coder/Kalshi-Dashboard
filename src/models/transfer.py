"""One row per day: how many bytes we moved to and from the database.

Storage had a growth line and transfer had nothing, so the first time anyone
learned about the transfer quota was when Neon closed every connection and
production stopped. A limit that kills the system at 100% needs a number long
before that.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class TransferSample(Base):
    __tablename__ = "transfer_samples"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sampled_on: Mapped[date] = mapped_column(Date, unique=True, index=True)
    bytes_in: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0)
    statements: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        default=lambda: datetime.now(timezone.utc),
    )
