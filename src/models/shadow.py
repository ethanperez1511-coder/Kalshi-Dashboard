"""Shadow maker orders: what a resting order WOULD have done.

Its own table on purpose. Shadow simulation must never touch `trades` — the
50-trade gate keeps accruing on the real, validated taker path, and a
simulation writing into the same record would make the gate mean two different
things at once.

Quantities are Numeric, not Integer. `count_fp` is fractional (1541 of 2299
observed prints were non-integer), so an integer column here would silently
truncate every fill it recorded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

_QTY = Numeric(18, 6)
_MONEY = Numeric(18, 6)


class ShadowMakerOrder(Base):
    __tablename__ = "shadow_maker_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(60), index=True, default="")
    model_name: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, default=None)
    side: Mapped[str] = mapped_column(String(8))

    intended_quantity: Mapped[Decimal] = mapped_column(_QTY)
    filled_quantity: Mapped[Decimal] = mapped_column(_QTY, default=Decimal("0"))

    # What the maker path did.
    start_price_cents: Mapped[int] = mapped_column(Integer)
    final_price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    steps_taken: Mapped[int] = mapped_column(Integer, default=0)
    cap_cents: Mapped[int] = mapped_column(Integer)
    capped: Mapped[bool] = mapped_column(default=False)

    # What the taker path would have cost on the SAME signal, at decision time.
    taker_price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)

    # Per-contract capture in cents, taker price minus maker fill price.
    capture_cents: Mapped[Optional[Decimal]] = mapped_column(_MONEY, nullable=True, default=None)
    maker_fee: Mapped[Optional[Decimal]] = mapped_column(_MONEY, nullable=True, default=None)
    taker_fee: Mapped[Optional[Decimal]] = mapped_column(_MONEY, nullable=True, default=None)

    # filled | partial | unfilled | not_placed | unproven
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    rest_start_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
