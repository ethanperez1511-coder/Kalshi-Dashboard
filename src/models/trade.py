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
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default=None)
    exit_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    # Entry trading fee in DOLLARS, recorded at fill time. Paper stores the
    # simulated Kalshi fee; live stores the real fee from the fills endpoint.
    # None means "written before fee recording existed" — settlement falls back
    # to the simulated estimate rather than pretending the fee was zero.
    entry_fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    # "simulated" (paper) | "kalshi_fills" (real) | "estimated" (fills lookup failed)
    entry_fee_source: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, default=None
    )
    # Which model produced the estimate behind this trade. Without it the
    # 50-trade paper gate can be satisfied entirely by one model while every
    # other has zero settled evidence — the count would say "validated" about a
    # system that is only validated in one corner.
    model_name: Mapped[Optional[str]] = mapped_column(
        String(60), nullable=True, default=None, index=True
    )
    # Which deployed commit executed this trade. Absent means it predates
    # deploy tracking, which is itself the signal — see is_legacy.
    deploy_sha: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True, default=None
    )
    # Excluded from the 50-trade gate and from calibration fits. The gate
    # evaluates the system that would go live, and a trade placed by superseded
    # code is not evidence about that system. The row stays — history is kept,
    # only its standing changes.
    # server_default, not just default: a Python-side default cannot be applied
    # by ALTER TABLE ADD COLUMN on a populated table, and the migration guard
    # correctly refuses NOT NULL columns it cannot backfill.
    is_legacy: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
