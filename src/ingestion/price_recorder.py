"""Record price snapshots — but only ones that will ever be read.

Ingest sees ~5,084 markets per 5-minute cycle. Writing all of them is 1.46M
rows/day, which at the measured ~436 bytes/row fills Neon's 0.5 GB tier in 2.8
days. Retention cannot rescue a write rate that high; the writes have to stop
at the source.

Two rules, both of which drop rows nothing downstream would have used:

  NO TRADE HISTORY   the scorer skips any market with last_price == 0 outright,
                     so a snapshot of one is written and never read. Measured on
                     the existing database, ~69% of snapshots are these.
  UNCHANGED          an illiquid market quoted at the same bid/ask/volume as its
                     last snapshot carries no new information. The previous row
                     already says the same thing, and its timestamp is the last
                     time that was true.

The staleness guard downstream keys on snapshot AGE, so suppressing unchanged
rows would eventually make a live-but-quiet market look stale. A heartbeat
interval bounds that: an unchanged market is still recorded periodically, so
"quiet" never becomes indistinguishable from "gone".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.price import PriceSnapshot
from src.trading_config import (
    SNAPSHOT_HEARTBEAT_MINUTES,
    SNAPSHOT_SKIP_UNCHANGED,
    SNAPSHOT_SKIP_UNTRADED,
)

logger = logging.getLogger(__name__)


def record_price_snapshot(
    engine: Engine, market_id: str, yes_bid: int, yes_ask: int,
    last_price: int, volume: int, now: Optional[datetime] = None,
) -> bool:
    """Write a snapshot if it carries information. Returns whether it wrote."""
    now = now or datetime.now(timezone.utc)

    # A market that has never traded is one the scorer refuses to price.
    if SNAPSHOT_SKIP_UNTRADED and last_price == 0 and volume == 0:
        return False

    with get_session(engine) as session:
        if SNAPSHOT_SKIP_UNCHANGED:
            previous = session.execute(
                select(
                    PriceSnapshot.yes_bid, PriceSnapshot.yes_ask,
                    PriceSnapshot.last_price, PriceSnapshot.volume,
                    PriceSnapshot.timestamp,
                )
                .where(PriceSnapshot.market_id == market_id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(1)
            ).first()

            if previous is not None:
                prev_bid, prev_ask, prev_last, prev_volume, prev_ts = previous
                unchanged = (
                    prev_bid == yes_bid and prev_ask == yes_ask
                    and prev_last == last_price and prev_volume == volume
                )
                if unchanged:
                    if prev_ts is not None and prev_ts.tzinfo is None:
                        prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                    age = (now - prev_ts).total_seconds() / 60.0 if prev_ts else 1e9
                    # Heartbeat: keep the freshness guard honest for a market
                    # that is quiet rather than gone.
                    if age < SNAPSHOT_HEARTBEAT_MINUTES:
                        return False

        session.add(PriceSnapshot(
            market_id=market_id, yes_bid=yes_bid, yes_ask=yes_ask,
            last_price=last_price, volume=volume, timestamp=now,
        ))
        session.commit()
    return True


def record_price_snapshots(engine: Engine, quotes: list, now: Optional[datetime] = None) -> int:
    """Batch form of record_price_snapshot. One read, one write.

    The per-market version opened a session, ran a SELECT for the previous
    snapshot, and committed — three round-trips each. At ~5,000 markets a cycle
    that is ~15,000 sequential round-trips to Neon, which is what exhausted the
    8-minute job budget.

    `quotes` is an iterable of (market_id, yes_bid, yes_ask, last_price, volume).
    Suppression rules are identical to the single-row path.
    """
    now = now or datetime.now(timezone.utc)
    quotes = list(quotes)
    if not quotes:
        return 0

    candidates = [
        q for q in quotes
        if not (SNAPSHOT_SKIP_UNTRADED and q[3] == 0 and q[4] == 0)
    ]
    if not candidates:
        return 0

    market_ids = [q[0] for q in candidates]
    previous = {}
    with get_session(engine) as session:
        if SNAPSHOT_SKIP_UNCHANGED:
            newest = (
                select(
                    PriceSnapshot.market_id.label("market_id"),
                    func.max(PriceSnapshot.id).label("snap_id"),
                )
                .where(PriceSnapshot.market_id.in_(market_ids))
                .group_by(PriceSnapshot.market_id)
                .subquery()
            )
            for row in session.execute(
                select(
                    PriceSnapshot.market_id, PriceSnapshot.yes_bid,
                    PriceSnapshot.yes_ask, PriceSnapshot.last_price,
                    PriceSnapshot.volume, PriceSnapshot.timestamp,
                ).join(newest, PriceSnapshot.id == newest.c.snap_id)
            ).all():
                previous[row[0]] = row[1:]

        rows = []
        for market_id, yes_bid, yes_ask, last_price, volume in candidates:
            prior = previous.get(market_id)
            if prior is not None:
                prev_bid, prev_ask, prev_last, prev_volume, prev_ts = prior
                unchanged = (
                    prev_bid == yes_bid and prev_ask == yes_ask
                    and prev_last == last_price and prev_volume == volume
                )
                if unchanged:
                    if prev_ts is not None and prev_ts.tzinfo is None:
                        prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                    age = (now - prev_ts).total_seconds() / 60.0 if prev_ts else 1e9
                    if age < SNAPSHOT_HEARTBEAT_MINUTES:
                        continue
            rows.append({
                "market_id": market_id, "yes_bid": yes_bid, "yes_ask": yes_ask,
                "last_price": last_price, "volume": volume, "timestamp": now,
            })

        if rows:
            session.bulk_insert_mappings(PriceSnapshot, rows)
        session.commit()
    return len(rows)
