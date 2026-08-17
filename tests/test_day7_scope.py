"""Measure by the axis that decides, not by Kalshi's label.

The first day-7 report put 5,547 prints across 11 markets into one bucket
called "General" — Kalshi's own category, and the same label that made
WeatherModel unreachable until commit e807f8d dispatched by claimed scope
instead. Per-category validation was ruled precisely so a liquid category
cannot carry an illiquid one through validation, and a single blended bucket
cannot support "maker validated for weather" or "for sports". It supports
nothing.

So the measurement is re-cut by the model that CLAIMS the market — the same
`claims()` the scorer dispatches on, imported rather than re-expressed, so the
buckets the maker rule is validated over are exactly the buckets it would
execute in.

The probe rate is retired here too, and that is the more consequential change.
Carrying a multi-level rate measured on one bucket into another IS pooling, by
the back door: it lets a liquid bucket vouch for a thin one through a constant.
A bucket without its own measured rate now gets NO N at all.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_engine, get_session
from src.execution.day7 import measure, scope_for_market
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'scope.db'}")
    Base.metadata.create_all(engine)
    return engine


def _market(engine, market_id, category="General"):
    with get_session(engine) as session:
        session.add(Market(
            market_id=market_id, title="t", category=category,
            close_date=NOW + dt.timedelta(days=2), status="active",
        ))
        session.commit()


def _prints(engine, market_id, n, hours=30, price="0.42"):
    """n trade prints spread over `hours` distinct hours, all live."""
    with get_session(engine) as session:
        for i in range(n):
            session.add(OrderbookDeltaRaw(
                market_ticker=market_id, msg_type="trade",
                ts_ms=1_000_000 + i * 37,
                payload='{"msg": {"yes_price_dollars": "%s"}}' % price,
                received_at=NOW - dt.timedelta(hours=i % hours),
            ))
        session.commit()


class TestScopeIsTheClaimingModel:
    def test_a_weather_ticker_scopes_to_the_weather_model(self):
        assert scope_for_market("KXHIGHNY-26AUG18-T90", "General") == "WeatherModel"

    def test_kalshis_general_label_does_not_decide(self):
        """The whole point: two markets Kalshi calls "General" land in
        different buckets, because the claiming model is the axis."""
        weather = scope_for_market("KXHIGHCHI-26AUG18-T85", "General")
        other = scope_for_market("KXJUNK-26AUG18-A", "General")

        assert weather == "WeatherModel"
        assert weather != other

    def test_a_general_non_weather_market_goes_to_its_own_claimant(self):
        """PolymarketModel legitimately claims "General", so that is the bucket
        — the model that would price it, which is the axis that decides."""
        assert scope_for_market("KXJUNK-26AUG18-A", "General") == "PolymarketModel"

    def test_a_market_nothing_claims_is_named_unclaimed(self):
        assert scope_for_market("KXJUNK-26AUG18-A", "Nonsense") == "unclaimed"

    def test_every_weather_series_scopes_together(self):
        from src.weather.stations import STATIONS

        scopes = {
            scope_for_market(f"{series}-26AUG18-T90", "General")
            for series in STATIONS
        }
        assert scopes == {"WeatherModel"}


class TestTheSplitIsReported:
    def test_weather_and_other_claimants_do_not_share_a_bucket(self, engine):
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _market(engine, "KXJUNK-26AUG18-A")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 300)
        _prints(engine, "KXJUNK-26AUG18-A", 300)

        results = measure(engine)

        assert "WeatherModel" in results
        assert "PolymarketModel" in results
        assert results["WeatherModel"]["prints"] == 300

    def test_hours_are_counted_per_scope_too(self, engine):
        """Hours and prints must describe the same bucket, or the rate is a
        ratio of two different populations."""
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 300, hours=30)

        results = measure(engine)

        assert results["WeatherModel"]["hours_recorded"] == 30

    def test_weather_series_detail_is_carried(self, engine):
        """One model, seven cities with different depth. The gate is per model;
        the per-series counts are shown so a single city cannot hide."""
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _market(engine, "KXHIGHMIA-26AUG18-T97")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 250)
        _prints(engine, "KXHIGHMIA-26AUG18-T97", 10)

        detail = measure(engine)["WeatherModel"]["by_series"]

        assert detail["KXHIGHNY"] == 250
        assert detail["KXHIGHMIA"] == 10


class TestProbeRateIsRetired:
    def test_a_thin_bucket_gets_no_rate_and_no_n(self, engine):
        """Below the print floor there is no measured rate, and borrowing one
        from another bucket is the cross-category carry the ruling forbids."""
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 50)

        stats = measure(engine)["WeatherModel"]

        assert stats["multi_level_rate"] is None
        assert stats["rate_source"] == "UNMEASURED"
        assert stats["recognised_fills_per_day"] is None
        assert stats["days_to_sample"] is None

    def test_a_measured_bucket_uses_its_own_rate(self, engine):
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 300)

        stats = measure(engine)["WeatherModel"]

        assert stats["rate_source"] == "MEASURED"
        assert stats["multi_level_rate"] is not None

    def test_one_buckets_rate_never_reaches_another(self, engine):
        """The regression the constant made possible."""
        _market(engine, "KXHIGHNY-26AUG18-T90")
        _market(engine, "KXJUNK-26AUG18-A")
        _prints(engine, "KXHIGHNY-26AUG18-T90", 300)
        _prints(engine, "KXJUNK-26AUG18-A", 20)

        results = measure(engine)

        assert results["WeatherModel"]["multi_level_rate"] is not None
        assert results["PolymarketModel"]["multi_level_rate"] is None

    def test_the_constant_is_gone_from_the_module(self):
        """Not merely unused — removed, so it cannot be reintroduced by a
        default argument somewhere."""
        from src.execution import day7

        assert not hasattr(day7, "PROBE_MULTI_LEVEL_RATE")
