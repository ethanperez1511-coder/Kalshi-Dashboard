"""Sync bridge: fetch live markets from Kalshi and persist them."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import Engine

from src.config import Settings
from src.ingestion.market_sync import sync_markets
from src.ingestion.price_recorder import record_price_snapshot
from src.ingestion.series_ingest import ingest_series
from src.kalshi.client import KalshiClient
from src.trading_config import (
    EVENT_FETCH_CAP,
    INGEST_EVENT_CATEGORIES,
    MARKET_FETCH_CAP,
    SERIES_FETCH_CAP,
    ingest_series_list,
)

logger = logging.getLogger(__name__)


async def _fetch_and_sync(engine: Engine, settings: Settings) -> int:
    client = KalshiClient.from_settings(settings)
    try:
        markets = await client.get_all_markets(max_markets=MARKET_FETCH_CAP)
        if INGEST_EVENT_CATEGORIES:
            # Non-sports markets never surface in the capped /markets walk —
            # pull them via the events feed (true category, nested markets).
            event_markets = await client.get_event_markets(max_markets=EVENT_FETCH_CAP)
            seen = {m.ticker for m in markets}
            markets.extend(m for m in event_markets if m.ticker not in seen)
        sync_markets(engine, markets)
        for m in markets:
            record_price_snapshot(
                engine,
                market_id=m.ticker,
                yes_bid=m.yes_bid,
                yes_ask=m.yes_ask,
                last_price=m.last_price,
                volume=m.volume,
            )

        # Series-targeted pass, on its own call path after the general ingest.
        # It shares none of the caps above, so nothing here can shrink the
        # coverage the rest of the pipeline already depends on.
        series = ingest_series_list()
        if series:
            coverage = await ingest_series(
                engine, client, series, max_markets=SERIES_FETCH_CAP,
            )
            return len(markets) + coverage.fetched

        return len(markets)
    finally:
        await client._http.aclose()


def ingest_live_markets(engine: Engine, settings: Settings) -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an async context (e.g. uvicorn) — use nest_asyncio or
        # run in a new thread to avoid "cannot call asyncio.run()" error.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            count = pool.submit(asyncio.run, _fetch_and_sync(engine, settings)).result()
    else:
        count = asyncio.run(_fetch_and_sync(engine, settings))

    logger.info(f"Ingested {count} live markets")
    return count
