"""Every change of live ramp stage: when, from, to, why, by whom."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class RampEvent(Base):
    __tablename__ = "ramp_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_stage: Mapped[int] = mapped_column(Integer)
    to_stage: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    by: Mapped[str] = mapped_column(String(60))
