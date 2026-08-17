"""Per-series bars: the evidence must be at the granularity of the switch.

General -> per-model -> per-series is one argument at descending scale. If the
allow-list is per series and the measurement is per model, LA licenses Denver
through the measurement instead of through the control — the same failure moved
one layer sideways.

So each of the seven temperature series carries its own hours, its own prints,
its own measured multi-level rate and its own N. AUS/PHIL/DEN failing their own
bars is the intended outcome.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_engine, get_session
from src.execution.day7 import measure, scope_for_market
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'series.db'}")
    Base.metadata.create_all(engine)
    return engine


def _market(engine, market_id, category="General"):
    with get_session(engine) as session:
        session.add(Market(
            market_id=market_id, title="t", category=category,
            close_date=NOW + dt.timedelta(days=2), status="active",
        ))
        session.commit()


def _prints(engine, market_id, n, hours=30):
    with get_session(engine) as session:
        for i in range(n):
            stamp = 1_000_000 + (i // 2)
            session.add(OrderbookDeltaRaw(
                market_ticker=market_id, msg_type="trade", ts_ms=stamp,
                payload='{"msg": {"yes_price_dollars": "%s"}}'
                        % ("0.42" if i % 2 else "0.41"),
                received_at=NOW - dt.timedelta(hours=i % hours),
            ))
        session.commit()


class TestScopeCarriesTheSeries:
    def test_weather_scopes_to_model_and_series(self):
        assert scope_for_market("KXHIGHLAX-26AUG19-T83", "General") == (
            "WeatherModel:KXHIGHLAX"
        )

    def test_two_cities_are_two_buckets(self):
        lax = scope_for_market("KXHIGHLAX-26AUG19-T83", "General")
        den = scope_for_market("KXHIGHDEN-26AUG19-T92", "General")

        assert lax != den

    def test_a_model_without_a_series_map_stays_model_level(self):
        """Only a model whose scope IS a series map gets split by series."""
        assert scope_for_market("KXJUNK-26AUG19-A", "General") == "PolymarketModel"

    def test_all_seven_series_are_distinct_buckets(self):
        from src.weather.stations import STATIONS

        scopes = {
            scope_for_market(f"{series}-26AUG19-T90", "General")
            for series in STATIONS
        }
        assert len(scopes) == len(STATIONS) == 7


class TestBarsAreIndependent:
    def test_a_deep_series_does_not_make_a_thin_one_projectable(self, engine):
        """The whole point. LAX must not license DEN."""
        _market(engine, "KXHIGHLAX-26AUG19-T83")
        _market(engine, "KXHIGHDEN-26AUG19-T92")
        _prints(engine, "KXHIGHLAX-26AUG19-T83", 400, hours=30)
        _prints(engine, "KXHIGHDEN-26AUG19-T92", 12, hours=4)

        results = measure(engine)
        lax = results["WeatherModel:KXHIGHLAX"]
        den = results["WeatherModel:KXHIGHDEN"]

        assert lax["rate_source"] == "MEASURED"
        assert lax["projectable"] is True
        assert den["rate_source"] == "UNMEASURED"
        assert den["projectable"] is False
        assert den["days_to_sample"] is None

    def test_hours_are_per_series_too(self, engine):
        _market(engine, "KXHIGHLAX-26AUG19-T83")
        _market(engine, "KXHIGHDEN-26AUG19-T92")
        _prints(engine, "KXHIGHLAX-26AUG19-T83", 60, hours=30)
        _prints(engine, "KXHIGHDEN-26AUG19-T92", 8, hours=4)

        results = measure(engine)

        assert results["WeatherModel:KXHIGHLAX"]["hours_recorded"] == 30
        assert results["WeatherModel:KXHIGHDEN"]["hours_recorded"] == 4

    def test_each_series_measures_its_own_rate(self, engine):
        _market(engine, "KXHIGHLAX-26AUG19-T83")
        _market(engine, "KXHIGHCHI-26AUG19-T85")
        _prints(engine, "KXHIGHLAX-26AUG19-T83", 400)
        _prints(engine, "KXHIGHCHI-26AUG19-T85", 400)

        results = measure(engine)

        for key in ("WeatherModel:KXHIGHLAX", "WeatherModel:KXHIGHCHI"):
            assert results[key]["rate_source"] == "MEASURED"
            assert results[key]["multi_level_rate"] is not None
