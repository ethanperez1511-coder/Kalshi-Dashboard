"""The guard must gate PRICING, not just a nightly job.

If this only ran in CI, a settlement repoint would be a red workflow while the
pipeline kept quoting the old target — which is exactly what happened between
2026-08-14 and 08-16.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import Market, TERMS_PARSED
from src.models.weather import WeatherCellFit
from src.weather.model import WeatherModel

NOW = dt.datetime(2026, 8, 17, 18, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()
TARGET = TODAY + dt.timedelta(days=1)
TICKER = f"KXHIGHNY-{TARGET:%y%b%d}".upper() + "-T90"

CURRENT_RULES = (
    "If the maximum temperature recorded at New York City (CLINYC) for "
    "Aug 18, 2026, is greater than 90 fahrenheit according to The Weather "
    "Company, then the market resolves to Yes."
)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(WeatherCellFit(
            station="KNYC", lead_days=1, predictor="mos", bias=0.0, sigma=3.0,
            brier_skill=0.2, reliability_slope=1.0, n_eval_pairs=120,
            brier=0.15, promoted=True, fitted_at=NOW - dt.timedelta(days=1),
        ))
        session.commit()
    return db_engine


def _market(engine, rules):
    with get_session(engine) as session:
        session.add(Market(
            market_id=TICKER, title="Will the maximum temperature be >90 deg?",
            category="General", close_date=NOW + dt.timedelta(days=2),
            status="active", series_ticker="KXHIGHNY",
            strike_direction="above", strike_value=90.0, strike_unit="F",
            terms_status=TERMS_PARSED, rules=rules,
        ))
        session.commit()


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _Iem:
    def get(self, url, params=None, timeout=None, **kwargs):
        run_date = dt.date.fromisoformat(params["runtime"][:10])
        rows = [
            {"ftime": f"{run_date + dt.timedelta(days=o):%Y-%m-%d} 00:00", "n_x": 92.0}
            for o in range(1, 7)
        ]
        return _Response(200, {"data": rows})


def _model():
    model = WeatherModel(http=_Iem())
    model._now = lambda: NOW
    return model


def test_verified_rules_still_price(engine):
    _market(engine, CURRENT_RULES)
    model = _model()

    assert model.estimate(TICKER, "t", 0.5, engine) is not None, dict(model.refusals)


def test_a_repointed_site_refuses_at_scoring_time(engine):
    """Not a CI failure three days later. A refusal, this cycle."""
    _market(engine, CURRENT_RULES.replace("CLINYC", "CLILGA"))
    model = _model()

    assert model.estimate(TICKER, "t", 0.5, engine) is None
    assert model.refusals["settlement_site_changed"] == 1


def test_an_unknown_authority_refuses(engine):
    _market(engine, CURRENT_RULES.replace("The Weather Company", "Acme Weather"))
    model = _model()

    assert model.estimate(TICKER, "t", 0.5, engine) is None
    assert model.refusals["settlement_authority_unrecognised"] == 1


def test_missing_rules_text_refuses_rather_than_assuming(engine):
    _market(engine, None)
    model = _model()

    assert model.estimate(TICKER, "t", 0.5, engine) is None
    assert model.refusals["settlement_rules_missing"] == 1


def test_the_guard_runs_before_any_http_call(engine):
    """A contract we will refuse must not cost an IEM request every cycle."""
    _market(engine, CURRENT_RULES.replace("CLINYC", "CLILGA"))

    class _Counting(_Iem):
        calls = 0

        def get(self, *a, **k):
            _Counting.calls += 1
            return super().get(*a, **k)

    model = WeatherModel(http=_Counting())
    model._now = lambda: NOW
    model.estimate(TICKER, "t", 0.5, engine)

    assert _Counting.calls == 0
