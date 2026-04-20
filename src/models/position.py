from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    side: Mapped[str] = mapped_column(String(10))  # "yes" or "no"
    entry_price: Mapped[int] = mapped_column(Integer)  # cents
    quantity: Mapped[int] = mapped_column(Integer)
    current_price: Mapped[int] = mapped_column(Integer)  # cents
    status: Mapped[str] = mapped_column(String(20), index=True)  # "open" or "closed"
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def unrealized_pnl(self) -> float:
        """(current - entry) * qty / 100 for yes; (entry - current) * qty / 100 for no."""
        if self.side == "yes":
            return (self.current_price - self.entry_price) * self.quantity / 100
        else:
            return (self.entry_price - self.current_price) * self.quantity / 100

    @property
    def cost_basis(self) -> float:
        """entry * qty / 100 for yes; (100 - entry) * qty / 100 for no."""
        if self.side == "yes":
            return self.entry_price * self.quantity / 100
        else:
            return (100 - self.entry_price) * self.quantity / 100
