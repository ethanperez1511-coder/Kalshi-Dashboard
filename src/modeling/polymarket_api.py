"""Client for the public Polymarket Gamma API (no key required)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://gamma-api.polymarket.com"


@dataclass
class PolyMarket:
    """A binary Polymarket market normalized to a single YES price."""
    question: str
    yes_price: float  # 0..1
    volume_usd: float


class PolymarketClient:
    """Fetches top open binary markets, ordered by volume. Caches per instance."""

    def __init__(self, max_markets: int = 1000):
        self._max_markets = max_markets
        self._cache: List[PolyMarket] | None = None

    def get_markets(self) -> List[PolyMarket]:
        if self._cache is not None:
            return self._cache

        markets: List[PolyMarket] = []
        offset = 0
        page = 100
        while len(markets) < self._max_markets:
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
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                pm = self._parse_row(row)
                if pm is not None:
                    markets.append(pm)
            offset += page

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
        )
