from __future__ import annotations

import asyncio
import logging
import random

from sqlalchemy import Engine

from src.database import get_session
from src.models.market import Market
from src.ingestion.price_recorder import record_price_snapshot

logger = logging.getLogger(__name__)


async def stream_demo_prices(engine: Engine, interval: float = 10.0) -> None:
    logger.info(f"Starting demo price stream (interval={interval}s)")
    while True:
        with get_session(engine) as session:
            markets = session.query(Market).filter_by(status="open").all()
            market_ids = [m.market_id for m in markets]
        for market_id in market_ids:
            price = random.randint(10, 90)
            spread = random.randint(1, 3)
            record_price_snapshot(
                engine=engine, market_id=market_id,
                yes_bid=price, yes_ask=price + spread,
                last_price=price + random.randint(0, spread),
                volume=random.randint(100, 5000),
            )
        await asyncio.sleep(interval)


async def stream_live_prices(ws_url: str, api_key: str, api_secret: str, engine: Engine) -> None:
    raise NotImplementedError(
        "Live WebSocket streaming requires Kalshi API credentials. "
        "Use stream_demo_prices for offline mode."
    )
