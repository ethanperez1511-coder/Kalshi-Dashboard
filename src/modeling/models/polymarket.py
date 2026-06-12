"""Model that prices Kalshi markets against matching Polymarket markets.

Polymarket is a real-money prediction market — its price on the same event is
an independent signal, exactly like sportsbook odds for sports parlays.

Matching is deliberately conservative: a wrong match is fabricated data, so
anything short of a clear, unambiguous title match produces no estimate.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Set

from sqlalchemy import Engine

from src.modeling.base import BaseModel, ModelResult, MODEL_TYPE_INDEPENDENT
from src.modeling.polymarket_api import PolyMarket, PolymarketClient
from src.trading_config import (
    POLYMARKET_MIN_SIMILARITY,
    POLYMARKET_MIN_VOLUME_USD,
    POLYMARKET_SCAN_LIMIT,
)

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "will", "the", "a", "an", "be", "in", "by", "of", "to", "on", "at",
    "before", "for", "is", "do", "does", "or", "and", "as", "with",
}

# Second-best match must trail the best by at least this much, or the
# match is ambiguous and we refuse to pick. Set wide enough that deadline
# variants of the same question ("in 2026" vs "before July 2026", sim ~0.8
# vs an exact 1.0) still count as ambiguous.
_AMBIGUITY_MARGIN = 0.25


def _normalize_tokens(title: str) -> Set[str]:
    words = re.findall(r"[a-z0-9.%]+", title.lower())
    return {w for w in words if w not in _STOPWORDS}


def _numbers_in(title: str) -> Set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", title))


def _similarity(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _match_market(
    title: str,
    candidates: List[PolyMarket],
    min_similarity: float = POLYMARKET_MIN_SIMILARITY,
) -> Optional[PolyMarket]:
    """Find the one Polymarket market for a Kalshi title, or None.

    Requirements: similarity above threshold, identical numeric tokens
    (thresholds/dates/years), and a clear winner over the runner-up.
    """
    tokens = _normalize_tokens(title)
    numbers = _numbers_in(title)

    scored = []
    for cand in candidates:
        sim = _similarity(tokens, _normalize_tokens(cand.question))
        if sim < min_similarity:
            continue
        if _numbers_in(cand.question) != numbers:
            # Same words, different threshold/date = a different market.
            continue
        scored.append((sim, cand))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < _AMBIGUITY_MARGIN:
        return None  # ambiguous — picking one would be a guess
    return scored[0][1]


class PolymarketModel(BaseModel):
    """Estimate probability from the matching Polymarket market's price."""

    CATEGORIES = {
        "Elections", "Politics", "Economics", "World", "Companies",
        "Entertainment", "Science and Technology", "Financials",
        "Climate and Weather", "Health", "Social", "General",
    }

    def __init__(self, poly_client: Optional[PolymarketClient] = None):
        self._client = poly_client or PolymarketClient(max_markets=POLYMARKET_SCAN_LIMIT)

    @property
    def category(self) -> str:
        return "Elections"

    def matches(self, category: str) -> bool:
        return category in self.CATEGORIES

    @property
    def model_type(self) -> str:
        return MODEL_TYPE_INDEPENDENT

    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        try:
            candidates = self._client.get_markets()
        except Exception:
            # Feed down: no estimate. Never substitute a number.
            logger.warning("Polymarket feed unavailable — no estimate", exc_info=True)
            return None

        liquid = [c for c in candidates if c.volume_usd >= POLYMARKET_MIN_VOLUME_USD]
        match = _match_market(title, liquid)
        if match is None:
            return None

        sim = _similarity(_normalize_tokens(title), _normalize_tokens(match.question))
        # Confidence grows with match quality and book depth, capped below the
        # level reserved for exact-data models.
        volume_factor = min(1.0, match.volume_usd / 1_000_000.0)
        confidence = min(0.85, 0.5 + 0.25 * sim + 0.1 * volume_factor)

        return ModelResult(
            market_id=market_id,
            p_model=match.yes_price,
            confidence=confidence,
            reasoning=(
                f"Polymarket match (sim={sim:.2f}, vol=${match.volume_usd:,.0f}): "
                f"\"{match.question[:80]}\" @ {match.yes_price:.2%}"
            ),
            data_sources=["polymarket"],
        )
