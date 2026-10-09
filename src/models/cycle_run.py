"""One row per trading cycle that ran: when, from which workflow, and whether
it finished. A schedule that does not fire produces no error anywhere, so the
only way to see one is to count what did run."""
from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class CycleRun(Base):
    __tablename__ = "cycle_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(60))
    # Total equity when the cycle ended: the series the bankroll-drop switch
    # reads (equity now vs its 24 h high).
    equity: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
