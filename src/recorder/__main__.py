"""Run the orderbook recorder.

    python -m src.recorder --duration 3300     # ~55 minutes

The pipeline host is a 5-minute cron, which cannot hold a websocket open. So
the recorder runs as its own scheduled job whose duration nearly fills its
interval — hourly job, 55-minute run — giving near-continuous coverage without
a long-lived process to run out of resources, the failure that killed the
Railway host.

Exits non-zero if it recorded nothing, so a silently dead recorder shows up as
a failed run rather than as a quiet gap discovered months later.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from src.config import Settings, require_production_database
from src.database import get_engine, verify_or_migrate
from src.kalshi.auth import KalshiAuth
from src.recorder.book_recorder import BookRecorder, markets_to_record
from src.run_summary import write_summary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Record the Kalshi orderbook feed.")
    parser.add_argument("--duration", type=float, default=3300.0)
    parser.add_argument("--max-markets", type=int, default=80)
    args = parser.parse_args(argv)

    settings = Settings()
    if settings.is_offline_mode:
        logger.error("No Kalshi credentials — recorder cannot run")
        return 1

    engine = get_engine(settings.DATABASE_URL)
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="the book recorder")

    markets = markets_to_record(engine, limit=args.max_markets)
    if not markets:
        write_summary("Book recorder: NO MARKETS to record", ok=False)
        logger.error(
            "No markets to record — nothing is scored or held, so there is "
            "nothing whose fills we would later need to simulate"
        )
        return 1

    logger.info("Recording %d markets for %.0fs", len(markets), args.duration)
    auth = KalshiAuth(
        api_key=settings.KALSHI_API_KEY,
        private_key_path=settings.KALSHI_PRIVATE_KEY_PATH,
        private_key_pem=settings.KALSHI_PRIVATE_KEY,
    )
    recorder = BookRecorder(engine, auth, markets)
    stats = asyncio.run(recorder.run(args.duration))

    logger.info(stats.summary())
    write_summary(
        f"Book recorder: {stats.written} messages, {len(stats.markets)} markets, "
        f"{stats.gaps} gaps, {stats.reconnects} reconnects",
        stats.summary(), ok=stats.written > 0,
    )
    if stats.written == 0:
        logger.error("Recorder wrote nothing — treating as failure, not as a quiet hour")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
