"""The fit loaded must be the fit for the run that produced the forecast.

With the fallback in place a target one day out can be served by a run issued
two days ago. The lead is then 2, not 1, and sigma at lead 2 is materially
wider — that is the whole point of fitting per lead. Loading the lead-1 fit for
a lead-2 forecast would be a precise statement about the wrong distribution,
and it would be invisible: the number produced looks exactly like a good one.

So `estimate` resolves the forecast FIRST and loads the fit for the lead the
forecast actually carries.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import Market, TERMS_PARSED
from src.models.weather import WeatherCellFit
from src.weather import mos
from src.weather.model import WeatherModel

NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.timezone.utc)   # before 12Z lands
TODAY = NOW.date()
TARGET = TODAY + dt.timedelta(days=1)
TICKER = f"KXHIGHNY-{TARGET:%y%b%d}".upper() + "-T90"


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id=TICKER, title="Will the maximum temperature be >90°?",
            category="General", close_date=NOW + dt.timedelta(days=2),
            status="active", series_ticker="KXHIGHNY",
            strike_direction="above", strike_value=90.0, strike_unit="F",
            terms_status=TERMS_PARSED,
        ))
        session.commit()
    return db_engine


def _fit(engine, lead_days, bias=0.0, sigma=3.0, fitted_days_ago=1):
    with get_session(engine) as session:
        session.add(WeatherCellFit(
            station="KNYC", lead_days=lead_days, predictor="mos",
            bias=bias, sigma=sigma, brier_skill=0.20, reliability_slope=1.0,
            n_eval_pairs=120, brier=0.15, promoted=True,
            fitted_at=NOW - dt.timedelta(days=fitted_days_ago),
        ))
        session.commit()


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _Iem:
    def __init__(self, published):
        self.published = set(published)

    def get(self, url, params=None, timeout=None, **kwargs):
        run_date = dt.date.fromisoformat(params["runtime"][:10])
        if run_date not in self.published:
            return _Response(404)
        rows = [
            {"ftime": f"{run_date + dt.timedelta(days=o):%Y-%m-%d} 00:00",
             "n_x": 92.0}
            for o in range(1, 7)
        ]
        return _Response(200, {"data": rows})


def _model(engine, http, now=NOW):
    model = WeatherModel(http=http)
    model._now = lambda: now
    return model


class TestFitMatchesTheRunUsed:
    def test_a_yesterday_run_uses_the_lead_2_fit(self, engine):
        """Target is 1 day out; the only published run is 2 days before it."""
        _fit(engine, lead_days=2, sigma=5.0)
        model = _model(engine, _Iem(published=[TODAY - dt.timedelta(days=1)]))

        result = model.estimate(TICKER, "t", 0.5, engine)

        assert result is not None, dict(model.refusals)
        assert "lead 2d" in result.reasoning

    def test_it_refuses_when_only_the_wrong_lead_is_fitted(self, engine):
        """A lead-1 fit must NOT be substituted for a lead-2 forecast."""
        _fit(engine, lead_days=1)
        model = _model(engine, _Iem(published=[TODAY - dt.timedelta(days=1)]))

        result = model.estimate(TICKER, "t", 0.5, engine)

        assert result is None
        assert any(k.startswith("cell_") for k in model.refusals)

    def test_todays_run_still_uses_the_lead_1_fit(self, engine):
        _fit(engine, lead_days=1)
        after_publication = NOW.replace(hour=18)
        model = _model(engine, _Iem(published=[TODAY]), now=after_publication)

        result = model.estimate(TICKER, "t", 0.5, engine)

        assert result is not None, dict(model.refusals)
        assert "lead 1d" in result.reasoning


class TestTheBlackoutIsGone:
    def test_a_morning_cycle_prices_instead_of_refusing(self, engine):
        """09:00 UTC: today's 12Z does not exist yet. This used to be
        `mos_unavailable` for every station, every lead, most of the day."""
        _fit(engine, lead_days=2)
        model = _model(engine, _Iem(published=[TODAY - dt.timedelta(days=1)]))

        assert model.estimate(TICKER, "t", 0.5, engine) is not None

    def test_no_published_run_at_all_still_refuses(self, engine):
        """Fallback is not fabrication. Nothing published means no price."""
        _fit(engine, lead_days=1)
        _fit(engine, lead_days=2)
        model = _model(engine, _Iem(published=[]))

        assert model.estimate(TICKER, "t", 0.5, engine) is None
        assert model.refusals["mos_unavailable"] == 1


class TestLeadBoundsAfterFallback:
    def test_a_fallback_that_pushes_past_the_max_lead_is_named(self, engine):
        """Target 3 days out served by a 2-day-old run is lead 5, beyond
        MAX_PRICEABLE_LEAD. That is a distinct event and gets its own counter,
        not silence and not a mislabelled cell refusal."""
        far_target = TODAY + dt.timedelta(days=3)
        far_ticker = f"KXHIGHNY-{far_target:%y%b%d}".upper() + "-T90"
        with get_session(engine) as session:
            session.add(Market(
                market_id=far_ticker, title="Will the maximum temperature be >90°?",
                category="General", close_date=NOW + dt.timedelta(days=4),
                status="active", series_ticker="KXHIGHNY",
                strike_direction="above", strike_value=90.0, strike_unit="F",
                terms_status=TERMS_PARSED,
            ))
            session.commit()
        _fit(engine, lead_days=5)
        model = _model(engine, _Iem(published=[TODAY - dt.timedelta(days=2)]))

        result = model.estimate(far_ticker, "t", 0.5, engine)

        assert result is None
        assert any("lead" in k for k in model.refusals), dict(model.refusals)
