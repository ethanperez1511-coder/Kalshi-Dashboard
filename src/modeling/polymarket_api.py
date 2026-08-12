"""Client for the public Polymarket Gamma API (no key required)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://gamma-api.polymarket.com"


@dataclass
class PolyMarket:
    """A binary Polymarket market normalized to a single YES price."""
    question: str
    yes_price: float  # 0..1
    volume_usd: float
    # Stable Polymarket identity. A human-approved Kalshi↔Polymarket mapping is
    # stored against this, so it must not be the question text — wording drifts.
    condition_id: str = ""
    # Resolution horizon. Two contracts can name the same event and still be
    # different questions: "next PM, ever" is not "next PM by end-2026". The
    # shorter-dated one is bounded below the longer, always in the same
    # direction, so the gap reads as edge rather than as a mismatch.
    end_date: Optional[datetime] = None


class PolymarketClient:
    """Fetches top open binary markets, ordered by volume. Caches per instance."""

    def __init__(self, max_markets: int = 1000):
        self._max_markets = max_markets
        self._cache: List[PolyMarket] | None = None

    # Gamma rejects pagination past a few thousand rows with a 422. That is the
    # end of the result set, not a failure — and a hard page cap makes the walk
    # terminate even if the API's behaviour changes again.
    MAX_PAGES = 40

    def get_markets(self) -> List[PolyMarket]:
        if self._cache is not None:
            return self._cache

        markets: List[PolyMarket] = []
        offset = 0
        page = 100

        for _ in range(self.MAX_PAGES):
            if len(markets) >= self._max_markets:
                break
            try:
                resp = httpx.get(
                    f"{_BASE_URL}/markets",
                    params={
                        "closed": "false",
                        "active": "true",
                        "limit": page,
                        "offset": offset,
                        "order": "volumeNum",
                        "ascending": "false",
                    },
                    timeout=15.0,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Polymarket: transport error at offset %d (%s) — keeping the "
                    "%d markets already fetched", offset, exc, len(markets),
                )
                break

            if resp.status_code == 422:
                # END OF RESULTS, not a retryable failure. Gamma answers 422
                # once the offset runs past what it will serve. Retrying is
                # pointless — the request is deterministic and will fail
                # identically — and re-walking from offset 0 is what turned this
                # into an infinite loop that consumed an entire 8-minute cycle.
                logger.info(
                    "Polymarket: pagination ends at offset %d (422) — %d markets",
                    offset, len(markets),
                )
                break
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "Polymarket: %d at offset %d — request is wrong, not retrying; "
                    "keeping %d markets", resp.status_code, offset, len(markets),
                )
                break
            if resp.status_code >= 500:
                logger.warning(
                    "Polymarket: %d at offset %d — keeping %d markets",
                    resp.status_code, offset, len(markets),
                )
                break

            rows = resp.json()
            if not rows:
                break
            for row in rows:
                pm = self._parse_row(row)
                if pm is not None:
                    markets.append(pm)
            offset += page

        # Cache even a partial or empty result. Leaving the cache unset is what
        # made every subsequent caller restart the whole walk; the markets are
        # ordered by volume descending, so a truncated list is the liquid head
        # of the book, which is the only part matching cares about.
        self._cache = markets[: self._max_markets]
        logger.info(f"Polymarket: loaded {len(self._cache)} binary markets")
        return self._cache

    def clear_cache(self):
        self._cache = None

    @staticmethod
    def _parse_row(row: dict) -> PolyMarket | None:
        """Normalize a Gamma API row to a binary YES price, or skip it."""
        try:
            outcomes = json.loads(row.get("outcomes", "[]"))
            prices = json.loads(row.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, TypeError):
            return None
        if len(outcomes) != 2 or len(prices) != 2:
            return None  # only binary Yes/No markets are comparable
        try:
            yes_idx = [o.lower() for o in outcomes].index("yes")
        except ValueError:
            return None
        try:
            yes_price = float(prices[yes_idx])
        except (ValueError, TypeError):
            return None
        question = row.get("question", "")
        if not question:
            return None
        return PolyMarket(
            question=question,
            yes_price=yes_price,
            volume_usd=float(row.get("volumeNum", 0.0) or 0.0),
            condition_id=str(row.get("conditionId") or row.get("id") or ""),
            end_date=_parse_end_date(row.get("endDate")),
        )


def _parse_end_date(raw) -> Optional[datetime]:
    """Gamma's ISO-8601 endDate, or None. An unparseable date is not a guess."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Polymarket: unparseable endDate %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
