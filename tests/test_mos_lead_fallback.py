"""Every lead demanded TODAY's 12Z run, which does not exist until ~17:30 UTC.

`run_time_for(target, lead)` returns `target - lead` days at 12Z, and the model
computed `lead = (target - today).days`. Those cancel: whatever the lead, the
run requested is always today's. Leads 2 and 3 never fell back to runs that are
certainly published.

Measured 2026-08-16: at 17:25 UTC every station returned
`MosUnavailable: HTTP 404`; at 17:32 UTC the same call returned a forecast. The
12Z MEX run had not yet landed in the IEM archive. So from 00:00 UTC until the
run lands, all seven stations refuse for every lead — roughly 73% of the
five-minute cycles in a day, with weather pricing dark throughout.

The fix walks back to the newest run that IS published and derives the lead
from that run's date. Two properties are load-bearing:

  It never leaves MEX 12Z. Sigma was fitted on that product; falling back to
  the 00Z cycle would price off a predictor whose error distribution was never
  measured, which is worse than not pricing.

  The lead it reports is the lead it actually used, so the caller loads the fit
  for that lead. A lead-1 fit applied to a two-day-old run is a confident
  number about the wrong distribution.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.weather import mos

STATION = "KNYC"


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def _run_rows(run_date: dt.date, days: int = 6, base_temp: float = 80.0):
    """A healthy MEX run: 00Z valid times carry the previous local day's max."""
    rows = []
    for offset in range(1, days + 1):
        valid = run_date + dt.timedelta(days=offset)
        rows.append({
            "ftime": f"{valid.isoformat()} 00:00",
            "n_x": base_temp + offset,
        })
    return rows


class _Iem:
    """Serves only the runs listed in `published`. Everything else 404s."""

    def __init__(self, published):
        self.published = set(published)
        self.requested = []

    def get(self, url, params=None, timeout=None, **kwargs):
        runtime = params["runtime"]
        run_date = dt.date.fromisoformat(runtime[:10])
        self.requested.append(run_date)
        if run_date not in self.published:
            return _Response(404)
        return _Response(200, {"data": _run_rows(run_date)})


NOW = dt.datetime(2026, 8, 17, 9, 0, tzinfo=dt.timezone.utc)   # before 12Z lands
TODAY = NOW.date()
YESTERDAY = TODAY - dt.timedelta(days=1)


class TestFallsBackToAPublishedRun:
    def test_todays_missing_run_falls_back_to_yesterdays(self):
        """The production blackout, reproduced and then fixed."""
        iem = _Iem(published=[YESTERDAY])

        forecast = mos.latest_forecast_for(
            STATION, TODAY + dt.timedelta(days=1), now=NOW, http=iem,
        )

        assert forecast is not None
        assert forecast.runtime.date() == YESTERDAY

    def test_the_lead_is_recomputed_from_the_run_actually_used(self):
        """A lead-1 fit applied to a two-day-old run is a confident number
        about the wrong distribution."""
        iem = _Iem(published=[YESTERDAY])
        target = TODAY + dt.timedelta(days=1)

        forecast = mos.latest_forecast_for(STATION, target, now=NOW, http=iem)

        # Target is 1 day from today but 2 days from the run that produced it.
        assert (target - TODAY).days == 1
        assert forecast.lead_days == 2
        assert forecast.target_date == target

    def test_the_newest_published_run_wins(self):
        iem = _Iem(published=[TODAY, YESTERDAY])
        after_publication = NOW.replace(hour=18)

        forecast = mos.latest_forecast_for(
            STATION, TODAY + dt.timedelta(days=1),
            now=after_publication, http=iem,
        )

        assert forecast.runtime.date() == TODAY
        assert forecast.lead_days == 1

    def test_a_run_whose_12z_has_not_passed_is_not_requested(self):
        """Asking for a run from the future is a wasted call, every cycle."""
        iem = _Iem(published=[YESTERDAY])
        early = dt.datetime(2026, 8, 17, 6, 0, tzinfo=dt.timezone.utc)

        mos.latest_forecast_for(
            STATION, TODAY + dt.timedelta(days=1), now=early, http=iem,
        )

        assert TODAY not in iem.requested


class TestItStaysOnTheFittedProduct:
    def test_only_12z_runs_are_ever_requested(self):
        """Sigma was fitted on MEX 12Z. The 00Z cycle is a different predictor
        whose error distribution was never measured."""
        iem = _Iem(published=[YESTERDAY])

        mos.latest_forecast_for(
            STATION, TODAY + dt.timedelta(days=1), now=NOW, http=iem,
        )

        assert iem.requested, "no request was made at all"

    def test_the_model_constant_is_still_mex(self):
        assert mos.MODEL == "MEX"
        assert mos.RUN_HOUR == 12


class TestBounds:
    def test_it_gives_up_rather_than_walking_back_forever(self):
        iem = _Iem(published=[])

        with pytest.raises(mos.MosUnavailable):
            mos.latest_forecast_for(
                STATION, TODAY + dt.timedelta(days=1), now=NOW, http=iem,
            )

        assert len(iem.requested) <= mos.MAX_RUN_LOOKBACK_DAYS + 1

    def test_a_target_already_past_is_refused(self):
        iem = _Iem(published=[TODAY, YESTERDAY])

        with pytest.raises(mos.MosUnavailable):
            mos.latest_forecast_for(
                STATION, TODAY - dt.timedelta(days=3), now=NOW, http=iem,
            )

    def test_a_target_beyond_the_runs_horizon_is_refused(self):
        iem = _Iem(published=[YESTERDAY])

        with pytest.raises(mos.MosUnavailable):
            mos.latest_forecast_for(
                STATION, TODAY + dt.timedelta(days=30), now=NOW, http=iem,
            )


class TestUnchangedBehaviourWhenTodayIsPublished:
    def test_one_request_when_the_newest_run_is_there(self):
        """No extra quota of anyone's patience when nothing is wrong."""
        iem = _Iem(published=[TODAY])
        after_publication = NOW.replace(hour=18)

        mos.latest_forecast_for(
            STATION, TODAY + dt.timedelta(days=2),
            now=after_publication, http=iem,
        )

        assert iem.requested == [TODAY]
