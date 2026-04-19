from __future__ import annotations
import logging
from typing import List
from sqlalchemy import Engine
from src.database import get_session
from src.kalshi.schemas import KalshiMarket
from src.models.market import Market

logger = logging.getLogger(__name__)


def sync_markets(engine: Engine, kalshi_markets: List[KalshiMarket]) -> int:
    new_count = 0
    with get_session(engine) as session:
        for km in kalshi_markets:
            existing = session.query(Market).filter_by(market_id=km.ticker).first()
            if existing:
                existing.title = km.title
                existing.category = km.category
                existing.status = km.status
                existing.close_date = km.close_time
                existing.rules = km.rules_primary
            else:
                market = Market(
                    market_id=km.ticker,
                    title=km.title,
                    category=km.category,
                    close_date=km.close_time,
                    status=km.status,
                    rules=km.rules_primary,
                )
                session.add(market)
                new_count += 1
        session.commit()
    logger.info(f"Synced {len(kalshi_markets)} markets ({new_count} new)")
    return new_count
