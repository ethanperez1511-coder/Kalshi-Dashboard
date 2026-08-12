"""Durable Kalshi↔Polymarket market mappings.

A cross-market price is only signal if the two markets resolve on the same
event. Fuzzy title matching cannot guarantee that, so every mapping ends up in
one of three states:

  approved — trust it; skip matching entirely and read the price
  blocked  — never match this pair again, whatever the similarity says
  pending  — uncertain; produce **no estimate** until a human decides

Pending is the default for anything the entity comparison could not affirm.
That is deliberate: a wrong mapping is fabricated data feeding position sizing,
so silence is the safe failure. Approvals are recorded once and reused forever,
which is what keeps the fail-closed policy from costing coverage permanently.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class MarketMatchMap(Base):
    __tablename__ = "market_match_map"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kalshi_market_id: Mapped[str] = mapped_column(Text, index=True, unique=True)
    poly_condition_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)  # approved|blocked|pending
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    # Human-readable titles, kept so the review queue can be rendered without
    # re-fetching either feed.
    kalshi_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    poly_question: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    # Which field disagreed, verbatim from the entity comparison.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    verdict: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)
    decided_by: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
