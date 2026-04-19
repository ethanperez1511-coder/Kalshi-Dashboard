from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import Engine
from src.database import get_session
from src.models.price import PriceSnapshot

logger = logging.getLogger(__name__)


def record_price_snapshot(engine: Engine, market_id: str, yes_bid: int, yes_ask: int,
                          last_price: int, volume: int) -> None:
    with get_session(engine) as session:
        snapshot = PriceSnapshot(
            market_id=market_id, yes_bid=yes_bid, yes_ask=yes_ask,
            last_price=last_price, volume=volume,
            timestamp=datetime.now(timezone.utc),
        )
        session.add(snapshot)
        session.commit()
    logger.debug(f"Recorded price for {market_id}: bid={yes_bid} ask={yes_ask}")
