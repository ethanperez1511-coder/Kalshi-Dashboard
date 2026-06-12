"""Tests for PolymarketModel — cross-platform price comparison.

Polymarket is a real-money prediction market with a free public API. For
Kalshi markets it can match (politics, world events, econ), its price is an
independent signal — same design as SportsOddsModel vs sportsbooks.

Integrity rules under test:
- No match → None. Ambiguous match → None. API failure → None.
- Never a fallback/heuristic number.
"""
from __future__ import annotations

import pytest

from src.modeling.models.polymarket import (
    PolymarketModel,
    _match_market,
    _normalize_tokens,
    _numbers_in,
)
from src.modeling.polymarket_api import PolyMarket


def _poly(question: str, yes_price: float, volume: float = 100_000.0) -> PolyMarket:
    return PolyMarket(question=question, yes_price=yes_price, volume_usd=volume)


class _FakePolyClient:
    def __init__(self, markets, fail=False):
        self._markets = markets
        self._fail = fail

    def get_markets(self):
        if self._fail:
            raise RuntimeError("API down")
        return self._markets


class TestNormalization:
    def test_stopwords_and_punctuation_dropped(self):
        tokens = _normalize_tokens("Will the Fed raise rates in July?")
        assert "will" not in tokens
        assert "the" not in tokens
        assert "fed" in tokens
        assert "raise" in tokens

    def test_numbers_extracted(self):
        nums = _numbers_in("Will CPI inflation be above 5.0% for 2026?")
        assert "5.0" in nums
        assert "2026" in nums


class TestMatching:
    def test_clear_match_found(self):
        candidates = [
            _poly("Will Jerome Powell be removed as Fed chair in 2026?", 0.08),
            _poly("Will Uzbekistan win the 2026 FIFA World Cup?", 0.0005),
        ]
        m = _match_market(
            "Will Jerome Powell be removed as Fed Chair in 2026?", candidates
        )
        assert m is not None
        assert m.yes_price == 0.08

    def test_no_match_returns_none(self):
        candidates = [_poly("Will Uzbekistan win the 2026 FIFA World Cup?", 0.0005)]
        assert _match_market("Will the Fed cut rates in July 2026?", candidates) is None

    def test_number_mismatch_blocks_match(self):
        """Same words, different threshold = different market. Never match."""
        candidates = [_poly("Will CPI inflation be above 4.0% for 2026?", 0.30)]
        assert (
            _match_market("Will CPI inflation be above 5.0% for 2026?", candidates)
            is None
        )

    def test_ambiguous_candidates_block_match(self):
        """Two near-identical candidates — picking one would be a guess."""
        candidates = [
            _poly("Will Trump pardon Diddy in 2026?", 0.20),
            _poly("Will Trump pardon Diddy before July 2026?", 0.35),
        ]
        assert _match_market("Will Trump pardon Diddy in 2026?", candidates) is None


class TestModelIntegrity:
    def test_matched_market_uses_polymarket_price(self):
        client = _FakePolyClient(
            [_poly("Will Jerome Powell be removed as Fed chair in 2026?", 0.08)]
        )
        model = PolymarketModel(client)
        result = model.estimate(
            market_id="KXPOWELL-26",
            title="Will Jerome Powell be removed as Fed chair in 2026?",
            current_price=15.0,
            engine=None,
        )
        assert result is not None
        assert abs(result.p_model - 0.08) < 1e-9
        assert 0.0 <= result.p_model <= 1.0
        assert result.model_type if hasattr(result, "model_type") else True

    def test_no_match_returns_no_estimate(self):
        client = _FakePolyClient([_poly("Completely unrelated question?", 0.5)])
        model = PolymarketModel(client)
        assert (
            model.estimate("M1", "Will the Fed cut rates in July?", 50.0, None) is None
        )

    def test_api_failure_returns_none(self):
        client = _FakePolyClient([], fail=True)
        model = PolymarketModel(client)
        assert (
            model.estimate("M1", "Will the Fed cut rates in July?", 50.0, None) is None
        )

    def test_low_volume_market_ignored(self):
        """Thin Polymarket books are noise, not signal."""
        client = _FakePolyClient(
            [_poly("Will the Fed cut rates in July 2026?", 0.4, volume=500.0)]
        )
        model = PolymarketModel(client)
        assert (
            model.estimate("M1", "Will the Fed cut rates in July 2026?", 50.0, None)
            is None
        )

    def test_model_is_independent_type(self):
        model = PolymarketModel(_FakePolyClient([]))
        assert model.model_type == "INDEPENDENT"

    def test_matches_multiple_categories(self):
        model = PolymarketModel(_FakePolyClient([]))
        for cat in ("Elections", "Politics", "Economics", "World"):
            assert model.matches(cat)
        assert not model.matches("Sports")


class TestRegistryDispatch:
    def test_registry_routes_elections_to_polymarket(self):
        from src.modeling.registry import ModelRegistry

        reg = ModelRegistry()
        names = [type(m).__name__ for m in reg.get_models_for("Elections")]
        assert "PolymarketModel" in names

    def test_registry_still_routes_sports(self):
        from src.modeling.registry import ModelRegistry

        reg = ModelRegistry()
        names = [type(m).__name__ for m in reg.get_models_for("Sports")]
        assert "SportsOddsModel" in names
        assert "PolymarketModel" not in names
