"""Model that prices Kalshi markets against matching Polymarket markets.

Polymarket is a real-money prediction market — its price on the same event is
an independent signal, exactly like sportsbook odds for sports parlays.

Matching is deliberately conservative: a wrong match is fabricated data, so
anything short of a clear, unambiguous title match produces no estimate.
"""
from __future__ import annotations

import logging
import re
from datetime import timezone
from typing import List, Optional, Set

from sqlalchemy import Engine

from src.database import get_session
from src.models.market import Market

from src.modeling.base import BaseModel, ModelResult, MODEL_TYPE_INDEPENDENT
from src.modeling.entities import compare, extract
from src.modeling.match_store import (
    get_decision,
    record_auto_approved,
    record_pending,
)
from src.modeling.polymarket_api import PolyMarket, PolymarketClient
from src.trading_config import (
    POLYMARKET_MAX_HORIZON_GAP_DAYS,
    POLYMARKET_MIN_SIMILARITY,
    POLYMARKET_MIN_VOLUME_USD,
    POLYMARKET_REQUIRE_ENTITY_MATCH as REQUIRE_ENTITY_MATCH,
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

        # 1. A recorded decision outranks any amount of string similarity.
        decision = get_decision(engine, market_id)
        if decision is not None:
            status, condition_id = decision
            if status == "blocked":
                return None
            if status == "pending":
                return None  # awaiting human review — stay silent
            if status == "approved":
                return self._from_approved(market_id, condition_id, candidates)

        liquid = [c for c in candidates if c.volume_usd >= POLYMARKET_MIN_VOLUME_USD]
        match = _match_market(title, liquid)
        if match is None:
            return None  # nothing plausible; nothing to review either

        sim = _similarity(_normalize_tokens(title), _normalize_tokens(match.question))

        # 2. Title similarity got us a candidate; entities decide whether it is
        # the same event. "CPI above 3%" and "CPI below 3%" score ~0.9 here.
        if REQUIRE_ENTITY_MATCH:
            verdict = compare(extract(title), extract(match.question))
            if not verdict.is_match:
                record_pending(
                    engine,
                    kalshi_market_id=market_id,
                    poly_condition_id=match.condition_id,
                    similarity=sim,
                    kalshi_title=title,
                    poly_question=match.question,
                    verdict=verdict.verdict,
                    reason="; ".join(verdict.reasons),
                )
                logger.info(
                    f"Polymarket match queued for review ({verdict.verdict}) for "
                    f"{market_id}: {'; '.join(verdict.reasons)}"
                )
                return None
            horizon = self._horizon_conflict(market_id, match, engine)
            if horizon is not None:
                record_pending(
                    engine,
                    kalshi_market_id=market_id,
                    poly_condition_id=match.condition_id,
                    similarity=sim,
                    kalshi_title=title,
                    poly_question=match.question,
                    verdict="conflict",
                    reason=horizon,
                )
                logger.info(f"Polymarket horizon mismatch for {market_id}: {horizon}")
                return None

            record_auto_approved(
                engine,
                kalshi_market_id=market_id,
                poly_condition_id=match.condition_id,
                similarity=sim,
                kalshi_title=title,
                poly_question=match.question,
            )

        return self._result(market_id, match, sim, source="entity match")

    def _horizon_conflict(self, market_id: str, match, engine) -> Optional[str]:
        """Reject a pair whose contracts resolve over materially different windows.

        Titles can be identical while the questions are not: Kalshi's
        "next Prime Minister of Israel" runs to 2045, the Polymarket contract of
        the same name force-resolves at the end of 2026. P(short-dated) is
        bounded above by P(long-dated) and the gap only ever points one way, so
        pricing one off the other manufactures a persistent one-sided edge that
        cannot be arbitraged and will not show up until settlement — years out.

        Returns a reason string on conflict, or None if the horizons agree,
        or if either is unknown (unknown is handled by the caller's normal
        entity verdict, not invented here).
        """
        if engine is None or match.end_date is None:
            return None
        with get_session(engine) as session:
            row = session.query(Market).filter_by(market_id=market_id).first()
            close_date = row.close_date if row else None
        if close_date is None:
            return None
        if close_date.tzinfo is None:
            close_date = close_date.replace(tzinfo=timezone.utc)

        gap_days = abs((close_date - match.end_date).total_seconds()) / 86400.0
        if gap_days <= POLYMARKET_MAX_HORIZON_GAP_DAYS:
            return None
        return (
            f"resolution horizon differs by {gap_days:.0f} days "
            f"(Kalshi closes {close_date.date()}, Polymarket ends "
            f"{match.end_date.date()}); limit is {POLYMARKET_MAX_HORIZON_GAP_DAYS} days"
        )

    def _from_approved(self, market_id, condition_id, candidates):
        """Read the price for a mapping a human (or a clean entity match) blessed."""
        match = next(
            (c for c in candidates if c.condition_id and c.condition_id == condition_id),
            None,
        )
        if match is None:
            # Approved counterpart is gone from the feed — resolved, delisted, or
            # renamed. Silence beats guessing at a replacement.
            logger.info(
                f"Approved Polymarket mapping for {market_id} not in feed — no estimate"
            )
            return None
        if match.volume_usd < POLYMARKET_MIN_VOLUME_USD:
            return None
        return self._result(market_id, match, sim=1.0, source="approved mapping")

    def _result(self, market_id: str, match, sim: float, source: str) -> ModelResult:
        # Confidence grows with match quality and book depth, capped below the
        # level reserved for exact-data models.
        volume_factor = min(1.0, match.volume_usd / 1_000_000.0)
        confidence = min(0.85, 0.5 + 0.25 * sim + 0.1 * volume_factor)
        return ModelResult(
            market_id=market_id,
            p_model=match.yes_price,
            confidence=confidence,
            reasoning=(
                f"Polymarket {source} (sim={sim:.2f}, vol=${match.volume_usd:,.0f}): "
                f"\"{match.question[:80]}\" @ {match.yes_price:.2%}"
            ),
            data_sources=["polymarket"],
        )
