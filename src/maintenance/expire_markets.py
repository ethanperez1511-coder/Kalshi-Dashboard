"""Mark markets closed once their close date has passed.

`sync_markets` is the only writer of `Market.status`, and it only writes rows
that came back in the current fetch. A market that closes simply stops being
returned, so its row keeps `status='open'` forever. Measured in production on
2026-08-13: 32,074 rows counted as open against a live universe of roughly
5,100 — about 27,000 corpses.

The tempting reconciliation — "anything absent from this fetch is closed" — is
wrong and dangerous: the fetch is capped, so absence means "beyond the cap" at
least as often as it means "gone", and that rule would close live markets we
still hold positions in.

`close_date` is the reconciliation that needs no fetch coverage. It is
exchange-published data we already store, and a market past its own close date
cannot be open. Nothing is inferred and nothing is fabricated.

This is bookkeeping, not trading: settlement reads positions and the exchange,
never this flag, so a row flipped here can neither open nor close a position.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Engine, and_, or_, update

from src.database import get_session
from src.models.market import Market

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("open", "active")


def expire_closed_markets(engine: Engine, now: Optional[datetime] = None) -> int:
    """Flip past-close markets from open/active to closed. Returns the count."""
    now = now or datetime.now(timezone.utc)
    with get_session(engine) as session:
        result = session.execute(
            update(Market)
            .where(
                and_(
                    Market.status.in_(OPEN_STATUSES),
                    Market.close_date.isnot(None),
                    Market.close_date < now,
                )
            )
            .values(status="closed")
        )
        session.commit()
        count = result.rowcount or 0
    if count:
        logger.info("Expired %d markets whose close date had passed", count)
    return count


def open_market_count(engine: Engine, now: Optional[datetime] = None) -> int:
    """Markets that are open AND have not passed their close date.

    The scoring funnel's denominator. Counting the raw status column made the
    denominator grow without bound and buried every real signal in the
    stale-snapshot bucket.
    """
    from sqlalchemy import func, select

    now = now or datetime.now(timezone.utc)
    with get_session(engine) as session:
        return session.execute(
            select(func.count()).select_from(Market).where(
                and_(
                    Market.status.in_(OPEN_STATUSES),
                    or_(Market.close_date.is_(None), Market.close_date >= now),
                )
            )
        ).scalar_one()
