"""Series-targeted ingest and its coverage reporting (Phase 2.0, 2026-08-11).

Daily weather contracts are invisible to both existing feeds — measured against
the live API, the first 3000 rows of `/markets` are 1549 General + 1451 Sports
with zero weather tickers, and the events feed carries only long-horizon
climate markets. They exist solely behind an explicit `series_ticker` query, so
without this path the weather model would have had nothing to score.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.database import Base, get_session
from src.ingestion.market_sync import sync_markets
from src.ingestion.series_ingest import coverage_from_db, ingest_series
from src.kalshi.schemas import KalshiMarket
from src.models.market import (
    Market,
    TERMS_NOT_APPLICABLE,
    TERMS_PARSED,
    TERMS_UNPARSED,
    TERMS_UNSUPPORTED,
)
from src.portfolio.attribution import (
    models_without_settled_evidence,
    settled_by_model,
    trades_by_model,
)
from src.models.trade import Trade


def _wx(ticker, title, subtitle, strike_type, floor=None, cap=None):
    return KalshiMarket(
        ticker=ticker, title=title, category="Climate and Weather",
        close_time=datetime(2026, 8, 13, 4, 59, tzinfo=timezone.utc),
        status="active", subtitle=subtitle, yes_sub_title=subtitle,
        strike_type=strike_type, floor_strike=floor, cap_strike=cap,
        yes_bid=40, yes_ask=44, last_price=42, volume=500,
    )


NYC_ABOVE = _wx(
    "KXHIGHNY-26AUG12-T90",
    "Will the **high temp in NYC** be >90° on Aug 12, 2026?",
    "91° or above", "greater", floor=90.0,
)
AUS_BELOW = _wx(
    "KXHIGHAUS-26AUG12-T99",
    "Will the **high temp in Austin** be <99° on Aug 12, 2026?",
    "98° or below", "less", cap=99.0,
)
# Readable, but a type we do not model yet. Four of every six live contracts
# look like this (measured 2026-08-11), so it must NOT be reported as a parse
# failure — that would be a modelling gap masquerading as a broken parser.
BUCKET = _wx(
    "KXHIGHPHIL-26AUG12-B90.5",
    "Will the **high temp in Philadelphia** be 90-91° on Aug 12, 2026?",
    "", "between", floor=90.0, cap=91.0,
)
# Genuinely unreadable: claims a direction but carries no strike to compare to.
BROKEN = _wx(
    "KXHIGHNY-26AUG12-TBAD",
    "Will the **high temp in NYC** be warmer on Aug 12, 2026?",
    "", "greater", floor=None, cap=None,
)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(engine, market_id):
    with get_session(engine) as s:
        row = s.query(Market).filter_by(market_id=market_id).first()
        if row is None:
            return None
        return type("Snap", (), {
            "strike_direction": row.strike_direction,
            "strike_value": row.strike_value,
            "strike_unit": row.strike_unit,
            "terms_status": row.terms_status,
            "series_ticker": row.series_ticker,
        })


# --------------------------------------------------------------------------
# 1. Terms are stored explicitly, per contract
# --------------------------------------------------------------------------

class TestTermsPersistence:
    def test_direction_is_stored_per_contract_not_per_city(self, engine):
        sync_markets(engine, [NYC_ABOVE, AUS_BELOW], series_ticker="KXHIGHNY")
        assert _market(engine, NYC_ABOVE.ticker).strike_direction == "above"
        assert _market(engine, AUS_BELOW.ticker).strike_direction == "below"

    def test_threshold_and_unit_are_stored(self, engine):
        sync_markets(engine, [NYC_ABOVE])
        row = _market(engine, NYC_ABOVE.ticker)
        assert row.strike_value == 90.0
        assert row.strike_unit == "F"
        assert row.terms_status == TERMS_PARSED

    def test_unreadable_contract_is_marked_unpriceable_not_defaulted(self, engine):
        sync_markets(engine, [BROKEN])
        row = _market(engine, BROKEN.ticker)
        assert row.terms_status == TERMS_UNPARSED
        assert row.strike_direction is None   # NOT defaulted to "above"
        assert row.strike_value is None

    def test_unmodelled_type_is_distinguished_from_unreadable(self, engine):
        """A `between` bucket is readable; we simply do not price intervals yet.
        Recording it as a parse failure would fire a parser alarm for a
        deliberate scope decision — and 4 of every 6 live contracts are these."""
        sync_markets(engine, [BUCKET])
        row = _market(engine, BUCKET.ticker)
        assert row.terms_status == TERMS_UNSUPPORTED
        assert row.strike_direction is None   # still not priceable
        assert row.strike_value is None

    def test_non_threshold_market_is_not_applicable(self, engine):
        other = KalshiMarket(
            ticker="KXNEXTISRAELPM-45JAN01-GEIS",
            title="Will Gadi Eisenkot be the next Prime Minister of Israel?",
            category="Politics",
            close_time=datetime(2045, 1, 1, tzinfo=timezone.utc), status="active",
        )
        sync_markets(engine, [other])
        assert _market(engine, other.ticker).terms_status == TERMS_NOT_APPLICABLE

    def test_resync_updates_terms_rather_than_leaving_them_stale(self, engine):
        sync_markets(engine, [BROKEN])
        assert _market(engine, BROKEN.ticker).terms_status == TERMS_UNPARSED
        fixed = _wx(
            BROKEN.ticker,
            "Will the **high temp in NYC** be >85° on Aug 12, 2026?",
            "86° or above", "greater", floor=85.0,
        )
        sync_markets(engine, [fixed])
        row = _market(engine, BROKEN.ticker)
        assert row.terms_status == TERMS_PARSED
        assert row.strike_direction == "above"


# --------------------------------------------------------------------------
# 2. Ingest is config-driven and reports coverage
# --------------------------------------------------------------------------

class TestSeriesIngest:
    def _client(self, mapping):
        client = AsyncMock()

        async def get_series_markets(series_ticker, **kw):
            return mapping.get(series_ticker, [])

        client.get_series_markets = get_series_markets
        return client

    @pytest.mark.asyncio
    async def test_fetches_every_configured_series(self, engine):
        client = self._client({"KXHIGHNY": [NYC_ABOVE], "KXHIGHAUS": [AUS_BELOW]})
        cov = await ingest_series(engine, client, ["KXHIGHNY", "KXHIGHAUS"])
        assert cov.fetched == 2
        assert cov.parsed == 2
        assert _market(engine, AUS_BELOW.ticker).series_ticker == "KXHIGHAUS"

    @pytest.mark.asyncio
    async def test_counts_exist_versus_parsed(self, engine):
        client = self._client({"KXHIGHNY": [NYC_ABOVE, BROKEN]})
        cov = await ingest_series(engine, client, ["KXHIGHNY"])
        assert cov.fetched == 2
        assert cov.parsed == 1
        assert cov.unparsed == 1
        assert cov.parse_rate == 0.5
        assert "1 UNREADABLE" in cov.summary()

    @pytest.mark.asyncio
    async def test_unsupported_types_do_not_depress_the_parse_rate(self, engine):
        """The live shape: one readable threshold plus four unmodelled buckets.
        Parse rate must read 100% — we read everything we claim to read."""
        client = self._client({"KXHIGHNY": [NYC_ABOVE, BUCKET, BUCKET, BUCKET]})
        cov = await ingest_series(engine, client, ["KXHIGHNY"])
        assert cov.parsed == 1
        assert cov.unsupported == 3
        assert cov.unparsed == 0
        assert cov.parse_rate == 1.0
        assert "unsupported type" in cov.summary()
        assert "UNREADABLE" not in cov.summary()

    @pytest.mark.asyncio
    async def test_one_failing_series_does_not_abort_the_rest(self, engine):
        client = AsyncMock()

        async def get_series_markets(series_ticker, **kw):
            if series_ticker == "KXBROKEN":
                raise RuntimeError("upstream 500")
            return [NYC_ABOVE]

        client.get_series_markets = get_series_markets
        cov = await ingest_series(engine, client, ["KXBROKEN", "KXHIGHNY"])
        assert cov.failures == ["KXBROKEN"]
        assert cov.fetched == 1

    @pytest.mark.asyncio
    async def test_empty_series_is_distinguishable_from_a_failure(self, engine):
        client = self._client({"KXHIGHNY": []})
        cov = await ingest_series(engine, client, ["KXHIGHNY"])
        assert cov.failures == []
        assert cov.per_series["KXHIGHNY"] == (0, 0)

    def test_coverage_survives_the_process(self, engine):
        sync_markets(engine, [NYC_ABOVE, AUS_BELOW, BROKEN, BUCKET])
        with get_session(engine) as s:
            for m in s.query(Market).all():
                m.status = "open"
            s.commit()
        assert coverage_from_db(engine) == {
            "priceable": 2, "unsupported": 1, "unreadable": 1,
        }


# --------------------------------------------------------------------------
# 3. Per-model attribution — the 50-trade gate must not be one model's record
# --------------------------------------------------------------------------

class TestModelAttribution:
    def _trade(self, engine, model_name, status="filled"):
        with get_session(engine) as s:
            s.add(Trade(
                market_id="M", side="yes", action="buy", price=50, quantity=1,
                p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
                position_size_dollars=0.5, confidence=0.8, reasoning="t",
                is_paper=True, status=status, model_name=model_name,
            ))
            s.commit()

    def test_counts_are_per_model(self, engine):
        self._trade(engine, "SportsOddsModel")
        self._trade(engine, "SportsOddsModel")
        self._trade(engine, "WeatherModel")
        assert trades_by_model(engine) == {"SportsOddsModel": 2, "WeatherModel": 1}

    def test_rows_predating_attribution_are_visible_not_dropped(self, engine):
        self._trade(engine, None)
        assert trades_by_model(engine) == {"unattributed": 1}

    def test_settled_is_distinct_from_placed(self, engine):
        """A placed trade is a decision; only a settled one is evidence."""
        self._trade(engine, "WeatherModel", status="filled")
        self._trade(engine, "SportsOddsModel", status="closed")
        assert trades_by_model(engine)["WeatherModel"] == 1
        assert settled_by_model(engine) == {"SportsOddsModel": 1}

    def test_flags_models_with_no_settled_evidence(self, engine):
        """The Phase 1.5 situation: the count climbs on one model alone."""
        for _ in range(50):
            self._trade(engine, "SportsOddsModel", status="closed")
        missing = models_without_settled_evidence(
            engine, ["SportsOddsModel", "PolymarketModel", "WeatherModel"],
        )
        assert missing == ["PolymarketModel", "WeatherModel"]
