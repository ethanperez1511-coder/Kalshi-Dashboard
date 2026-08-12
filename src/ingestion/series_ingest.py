"""Series-targeted ingest, and an honest count of what we could read.

Daily weather contracts are reachable only by explicit series query, so they
arrive through here rather than through either general feed. The coverage
numbers this returns answer the question that matters before any modelling
starts: of the contracts that exist, how many can we actually price?

A contract whose terms will not parse is counted, logged and stored as
unpriceable. It is never quietly dropped — a silent drop looks identical to
"the series was empty today", and those need different responses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List

from sqlalchemy import Engine

from src.ingestion.market_sync import sync_markets
from src.ingestion.price_recorder import record_price_snapshots
from src.models.market import TERMS_PARSED, TERMS_UNPARSED, TERMS_UNSUPPORTED
from src.weather.terms import (
    is_temperature_market,
    is_unsupported_type,
    parse_contract_terms,
)

logger = logging.getLogger(__name__)


@dataclass
class SeriesCoverage:
    """How many contracts a series listed, and how many we could read."""

    fetched: int = 0
    parsed: int = 0
    unparsed: int = 0
    unsupported: int = 0
    per_series: Dict[str, tuple] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    @property
    def parse_rate(self) -> float:
        """Share of READABLE contracts we read. Unsupported types are excluded:
        they are a modelling gap, not a parsing failure, and folding them in
        would make a deliberate scope decision look like a broken parser."""
        readable = self.parsed + self.unparsed
        return (self.parsed / readable) if readable else 1.0

    def summary(self) -> str:
        if not self.fetched:
            return "series ingest: no markets returned"
        parts = [
            f"series ingest: {self.fetched} fetched",
            f"{self.parsed} priceable",
        ]
        if self.unsupported:
            parts.append(f"{self.unsupported} unsupported type (not modelled yet)")
        if self.unparsed:
            parts.append(f"{self.unparsed} UNREADABLE")
        parts.append(f"{self.parse_rate:.0%} of readable contracts parsed")
        return ", ".join(parts)


async def ingest_series(
    engine: Engine, client, series_tickers: List[str], max_markets: int = 500,
) -> SeriesCoverage:
    coverage = SeriesCoverage()

    for ticker in series_tickers:
        try:
            markets = await client.get_series_markets(ticker, max_markets=max_markets)
        except Exception:
            # One dead series must not take the rest of ingest with it.
            logger.warning("Series %s: fetch failed", ticker, exc_info=True)
            coverage.failures.append(ticker)
            continue

        if not markets:
            coverage.per_series[ticker] = (0, 0)
            continue

        weather = [m for m in markets if is_temperature_market(m)]
        unsupported = sum(1 for m in weather if is_unsupported_type(m))
        parsed = sum(
            1 for m in weather
            if not is_unsupported_type(m) and parse_contract_terms(m) is not None
        )
        unparsed = len(weather) - parsed - unsupported

        sync_markets(engine, markets, series_ticker=ticker)
        record_price_snapshots(
            engine,
            [(m.ticker, m.yes_bid, m.yes_ask, m.last_price, m.volume) for m in markets],
        )

        coverage.fetched += len(markets)
        coverage.parsed += parsed
        coverage.unparsed += unparsed
        coverage.unsupported += unsupported
        coverage.per_series[ticker] = (len(markets), parsed)

        if unparsed:
            logger.warning(
                "Series %s: %d/%d weather contracts UNREADABLE — not priceable",
                ticker, unparsed, len(weather),
            )

    logger.info(coverage.summary())
    return coverage


def coverage_from_db(engine: Engine) -> Dict[str, int]:
    """Stored coverage, for the daily digest — survives the process."""
    from sqlalchemy import func, select
    from src.database import get_session
    from src.models.market import Market

    with get_session(engine) as session:
        rows = session.execute(
            select(Market.terms_status, func.count())
            .where(Market.status.in_(("open", "active")))
            .group_by(Market.terms_status)
        ).all()
    counts = {status: count for status, count in rows}
    return {
        "priceable": counts.get(TERMS_PARSED, 0),
        "unsupported": counts.get(TERMS_UNSUPPORTED, 0),
        "unreadable": counts.get(TERMS_UNPARSED, 0),
    }
