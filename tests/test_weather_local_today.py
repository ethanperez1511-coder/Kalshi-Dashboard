"""The lead gate counts days in the STATION's calendar, not UTC's.

Settlement is the local calendar day (a station's `timezone`). The gate used
the UTC date, so from 00:00 UTC until local midnight (four hours in New York,
seven in Phoenix) the next local day's ladder read as lead 0 and was refused
`lead_past`. Seen in production: the manual cycle started 23:58 UTC on
2026-10-06 and scored just after 00:00 UTC (20:00 in New York); it refused all
76 threshold contracts, including the whole
Oct 7 ladder, which was a lead-1 contract with the Oct 6 12Z run in hand.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import TERMS_PARSED, Market
from src.weather import mos
from src.weather.model import WeatherModel


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        for ticker, series in (("KXHIGHNY-26OCT07-T70", "KXHIGHNY"),
                               ("KXHIGHTPHX-26OCT07-T95", "KXHIGHTPHX")):
            s.add(Market(
                market_id=ticker, title="t", category="General",
                close_date=dt.datetime(2026, 10, 8, 5, tzinfo=dt.timezone.utc),
                status="active", series_ticker=series, strike_direction="above",
                strike_value=70.0, strike_unit="F", terms_status=TERMS_PARSED,
            ))
        s.commit()
    return db_engine


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Past the lead gate the model fetches MOS; stop it there, observably."""
    def unavailable(*a, **k):
        raise mos.MosUnavailable("stubbed: the test is about the gate before this")

    monkeypatch.setattr(mos, "latest_forecast_for", unavailable)
    import src.weather.model as wm
    monkeypatch.setattr(wm, "verify_settlement", lambda *a, **k: (True, ""))


def _refusals(engine, ticker, now):
    model = WeatherModel(now=lambda: now)
    model.estimate(ticker, "t", 0.5, engine)
    return model.refusals


def test_new_york_evening_after_utc_midnight_is_still_lead_one(engine):
    """The production cycle started 23:58:50 UTC; after install, migrations
    and ingest it scored at about 00:00:30 UTC, past the UTC date change."""
    now = dt.datetime(2026, 10, 7, 0, 0, 30, tzinfo=dt.timezone.utc)  # 20:00 EDT Oct 6
    r = _refusals(engine, "KXHIGHNY-26OCT07-T70", now)
    assert r["lead_past"] == 0 and r["mos_unavailable"] == 1


def test_new_york_just_past_utc_midnight(engine):
    now = dt.datetime(2026, 10, 7, 2, 0, tzinfo=dt.timezone.utc)     # 22:00 EDT Oct 6
    assert _refusals(engine, "KXHIGHNY-26OCT07-T70", now)["lead_past"] == 0


def test_phoenix_late_evening_is_still_lead_one(engine):
    now = dt.datetime(2026, 10, 7, 6, 30, tzinfo=dt.timezone.utc)    # 23:30 MST Oct 6
    assert _refusals(engine, "KXHIGHTPHX-26OCT07-T95", now)["lead_past"] == 0


def test_after_local_midnight_the_same_contract_is_lead_zero(engine):
    now = dt.datetime(2026, 10, 7, 4, 30, tzinfo=dt.timezone.utc)    # 00:30 EDT Oct 7
    assert _refusals(engine, "KXHIGHNY-26OCT07-T70", now)["lead_past"] == 1
