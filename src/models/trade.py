from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    side: Mapped[str] = mapped_column(String(10))  # "yes" or "no"
    action: Mapped[str] = mapped_column(String(10))  # "buy" or "sell"
    price: Mapped[int] = mapped_column(Integer)  # cents
    quantity: Mapped[int] = mapped_column(Integer)
    p_model: Mapped[float] = mapped_column(Float)
    implied_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    net_ev: Mapped[float] = mapped_column(Float)
    position_size_dollars: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(Text)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    exit_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
