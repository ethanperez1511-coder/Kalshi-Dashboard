"""Latched trading halts. Active while cleared_at is NULL; only a human clears
one (maintenance --clear-halt --confirm CLEAR-HALT)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class HaltEvent(Base):
    __tablename__ = "halt_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    switch: Mapped[str] = mapped_column(String(40), index=True)
    detail: Mapped[str] = mapped_column(Text)
    tripped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cleared_by: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
