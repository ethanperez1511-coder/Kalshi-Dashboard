"""Raw orderbook feed capture. Append-only, consumed by nothing yet.

Kalshi serves no historical book. The trade tape reaches back ~60 days, the
book reaches back zero — so every day this is not running is a day of
validation window that cannot be recovered later. That is the entire reason
this table exists before the thing that reads it.

Stored raw on purpose. The simulator's reconstruction logic will change as it
is built and corrected, and re-deriving it from the original messages must
always be possible. Nothing here is normalised, deduplicated, or interpreted:
`payload` is the message exactly as received.

Two message types share the channel and both are kept:
  snapshot  full ladder, the anchor a delta sequence is applied to
  delta     one price level's change
A delta stream without its anchoring snapshot reconstructs nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class OrderbookDeltaRaw(Base):
    __tablename__ = "orderbook_delta_raw"
    __table_args__ = (
        # Reconstruction always walks one market in sequence order.
        Index("ix_obd_market_seq", "market_ticker", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(100), index=True)
    msg_type: Mapped[str] = mapped_column(String(24))       # snapshot | delta | trade
    sid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, default=None)

    # Denormalised for cheap querying. `payload` remains authoritative.
    side: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, default=None)
    price_dollars: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    delta_fp: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    ts_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, default=None)

    payload: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class OrderbookGap(Base):
    """A break in a subscription's sequence numbers.

    Recorded rather than repaired. A gap means the book cannot be reconstructed
    across it, so any simulated fill spanning one is unusable — and silently
    stitching the sequence back together would hide exactly that.
    """

    __tablename__ = "orderbook_gaps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(100), index=True)
    sid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    expected_seq: Mapped[int] = mapped_column(BigInteger)
    received_seq: Mapped[int] = mapped_column(BigInteger)
    missing: Mapped[int] = mapped_column(Integer)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
