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


def filter_ingestable(
    markets: Sequence, counts: Optional[Dict[str, int]] = None,
) -> List:
    """Drop markets whose series is excluded, tallying what was dropped."""
    kept = []
    for market in markets:
        series = series_of(getattr(market, "ticker", ""))
        if series in EXCLUDED_SERIES:
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
    """
    total = len(markets)
    if not total:
        return []

    tally: Dict[str, int] = {}
    for market in markets:
        series = series_of(getattr(market, "ticker", ""))
        if not series or series in EXCLUDED_SERIES:
            continue
        tally[series] = tally.get(series, 0) + 1

    return sorted(
        (
            (series, count, count / total)
            for series, count in tally.items()
            if count / total > threshold
        ),
        key=lambda row: -row[1],
    )
