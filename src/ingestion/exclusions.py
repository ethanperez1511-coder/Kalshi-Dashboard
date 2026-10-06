"""Series the ingest refuses to persist.

Measured 2026-08-17: 374,000 of 376,000 rows counted as open markets were
KXMVECROSSCATEGORY (218k) and KXMVESPORTSMULTIGAMEEXTENDED (156k) — parlay
combinations Kalshi mints continuously, 123,000 new rows on 2026-08-15 alone,
against a scorer that reached 2,270 markets. At ~60 MB/day with 126 MB of
headroom the free tier had roughly two days left.

No model prices a cross-category parlay and none is planned to. These rows are
not history being kept for later, they are a firehose being written to disk, so
neither retention nor archival is the right instrument — the write has to not
happen. Filtering here drops the market row and its price snapshot together,
which is where the bytes actually are.

Two deliberate properties:

  CONFIGURED, NOT CONSTANT   `TRADING_EXCLUDED_SERIES` so the next firehose is
                             a deploy of one environment variable, not a patch.
  COUNTED, NOT SILENT        the counts ride the funnel every cycle. An
                             invisible filter is how a legitimate series gets
                             dropped for a month with nobody noticing.

Matching is on the whole series token, never a prefix: "KXHIGH" as a prefix
rule would silently take out every weather contract the system trades.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from src.trading_config import EXCLUDED_SERIES_LIST, SERIES_CONCENTRATION_WARN

logger = logging.getLogger(__name__)

EXCLUDED_SERIES = frozenset(s.strip().upper() for s in EXCLUDED_SERIES_LIST if s.strip())


def series_of(ticker: Optional[str]) -> str:
    """The series token of a Kalshi ticker: everything before the first dash."""
    if not ticker:
        return ""
    return str(ticker).split("-", 1)[0].upper()


def is_excluded_series(ticker: Optional[str]) -> bool:
    return series_of(ticker) in EXCLUDED_SERIES


def excluded_as(market) -> Optional[str]:
    """The series name to tally an excluded market under, or None if kept.

    Excluded when its own series is listed, OR when the parlay collection
    Kalshi declares for it (`mve_collection_ticker`) belongs to a listed
    series. Kalshi minted KXMVECROSSCATEGORY0 into the KXMVECROSSCATEGORY-R
    collection (2026-10-06): a new name for the same firehose. Following the
    collection catches every such sibling without a prefix rule. The tally
    uses the market's own series, so a new sibling shows up by name.
    """
    series = series_of(getattr(market, "ticker", ""))
    if series in EXCLUDED_SERIES:
        return series
    collection = getattr(market, "mve_collection_ticker", None)
    if collection and series_of(collection) in EXCLUDED_SERIES:
        return series
    return None


def filter_ingestable(
    markets: Sequence, counts: Optional[Dict[str, int]] = None,
) -> List:
    """Drop excluded markets, tallying what was dropped."""
    kept = []
    for market in markets:
        series = excluded_as(market)
        if series is not None:
            if counts is not None:
                counts[series] = counts.get(series, 0) + 1
            continue
        kept.append(market)
    return kept


def concentration_warnings(
    markets: Sequence, threshold: float = SERIES_CONCENTRATION_WARN,
) -> List[Tuple[str, int, float]]:
    """Series taking more than `threshold` of one fetch, largest first.

    The exclusion list only knows about the firehose that already happened.
    This is the shape of the problem itself: a series that suddenly dominates a
    fetch is either a new parlay mint or a fetch that has stopped paginating,
    and both are worth a number in the log long before they are worth an
    emergency. Series already excluded are skipped — repeating a handled
    problem every cycle trains the operator to ignore the line.

    The share is of the rows that will actually be WRITTEN. Dividing by the
    whole fetch let the excluded parlays dilute a new mint: 150 rows behind
    600 excluded ones read as 14% when they were a third of the write.
    """
    tally: Dict[str, int] = {}
    for market in markets:
        series = series_of(getattr(market, "ticker", ""))
        if not series or excluded_as(market) is not None:
            continue
        tally[series] = tally.get(series, 0) + 1

    total = sum(tally.values())
    if not total:
        return []

    return sorted(
        (
            (series, count, count / total)
            for series, count in tally.items()
            if count / total > threshold
        ),
        key=lambda row: -row[1],
    )
