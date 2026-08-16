"""Sync bridge: fetch live markets from Kalshi and persist them."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import Engine

from src.config import Settings
from src.deadline import Deadline
from src.ingestion.exclusions import concentration_warnings, filter_ingestable
from src.ingestion.market_sync import sync_markets
from src.ingestion.price_recorder import record_price_snapshots
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


async def _fetch_and_sync(
    engine: Engine, settings: Settings, deadline: Deadline = None,
    excluded: dict = None,
) -> int:
    deadline = deadline or Deadline.none("ingest")
    client = KalshiClient.from_settings(settings)
    try:
        markets = await client.get_all_markets(max_markets=MARKET_FETCH_CAP)
        if INGEST_EVENT_CATEGORIES and not deadline.expired():
            # Non-sports markets never surface in the capped /markets walk —
            # pull them via the events feed (true category, nested markets).
            event_markets = await client.get_event_markets(max_markets=EVENT_FETCH_CAP)
            seen = {m.ticker for m in markets}
            markets.extend(m for m in event_markets if m.ticker not in seen)

        # Filtered BEFORE the write, not after: an excluded market must cost
        # neither a `markets` row nor a price snapshot. Filtering downstream of
        # sync_markets would leave the bytes exactly where the problem is.
        for series, count, share in concentration_warnings(markets):
            logger.warning(
                "Series %s is %.0f%% of this fetch (%d markets) — if it is a "
                "parlay mint it belongs in TRADING_EXCLUDED_SERIES",
                series, 100 * share, count,
            )
        markets = filter_ingestable(
            markets, excluded if excluded is not None else {},
        )
        if excluded:
            logger.info(
                "Ingest excluded %d markets: %s",
                sum(excluded.values()),
                ", ".join(f"{k}={v}" for k, v in sorted(excluded.items())),
            )

        sync_markets(engine, markets)
        record_price_snapshots(
            engine,
            [(m.ticker, m.yes_bid, m.yes_ask, m.last_price, m.volume) for m in markets],
        )

        # Series-targeted pass, on its own call path after the general ingest.
        # It shares none of the caps above, so nothing here can shrink the
        # coverage the rest of the pipeline already depends on.
        series = ingest_series_list()
        if series and not deadline.expired():
            coverage = await ingest_series(
                engine, client, series, max_markets=SERIES_FETCH_CAP, deadline=deadline,
            )
            return len(markets) + coverage.fetched

        return len(markets)
    finally:
        await client._http.aclose()


def ingest_live_markets(
    engine: Engine, settings: Settings, deadline: Deadline = None,
    excluded: dict = None,
) -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an async context (e.g. uvicorn) — use nest_asyncio or
        # run in a new thread to avoid "cannot call asyncio.run()" error.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            count = pool.submit(
                asyncio.run, _fetch_and_sync(engine, settings, deadline, excluded),
            ).result()
    else:
        count = asyncio.run(_fetch_and_sync(engine, settings, deadline, excluded))

    logger.info(f"Ingested {count} live markets")
    return count
