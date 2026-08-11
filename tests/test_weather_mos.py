"""MOS guidance ingest (Phase 2.1, 2026-08-11).

MOS is the production predictor AND the series σ is fitted on — deliberately
the same product, because NWS's gridpoint API has no archive and fitting on one
product while pricing off another measures a different model's errors than the
one placing trades.

Everything here is about refusing to price on partial data. A short or
null-filled run must raise, not return the good half: downstream, a
partially-filled forecast is indistinguishable from a complete one.

Day alignment is verified against real values (KPHL run 2025-08-01 12Z vs
GHCN-Daily truth): 00Z valid time on D+1 is the maximum for local day D.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.weather.mos import (
    MAX_LEAD_DAYS,
    MIN_LEADS,
    MosUnavailable,
    fetch_run,
    parse_run,
    run_time_for,
)

RUNTIME = dt.datetime(2025, 8, 1, 12, tzinfo=dt.timezone.utc)

# Verbatim shape from the live IEM response.
REAL_ROWS = [
    {"ftime": "2025-08-02 12:00", "n_x": 64},   # overnight MIN — ignored
    {"ftime": "2025-08-03 00:00", "n_x": 82},   # local 2025-08-02 max
    {"ftime": "2025-08-03 12:00", "n_x": 64},
    {"ftime": "2025-08-04 00:00", "n_x": 84},   # local 2025-08-03 max
    {"ftime": "2025-08-04 12:00", "n_x": 66},
    {"ftime": "2025-08-05 00:00", "n_x": 86},
    {"ftime": "2025-08-05 12:00", "n_x": 68},
    {"ftime": "2025-08-06 00:00", "n_x": 88},
    {"ftime": "2025-08-07 00:00", "n_x": 90},
]


class _Http:
    def __init__(self, payload, status=200, raises=None):
        self.payload, self.status, self.raises = payload, status, raises
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        if self.raises:
            raise self.raises
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = self.status
        resp.json.return_value = self.payload
        return resp


class TestDayAlignment:
    def test_00z_valid_time_is_the_previous_local_days_max(self):
        by_date = {f.target_date: f.max_temp_f for f in parse_run("KPHL", RUNTIME, REAL_ROWS)}
        assert by_date[dt.date(2025, 8, 2)] == 82
        assert by_date[dt.date(2025, 8, 3)] == 84

    def test_12z_valid_times_are_ignored_as_overnight_minima(self):
        """64°F is the overnight low. Treating it as a daily max would put the
        forecast ~20°F under truth and read as enormous edge."""
        assert all(f.max_temp_f >= 82 for f in parse_run("KPHL", RUNTIME, REAL_ROWS))

    def test_lead_days_count_from_the_run_date(self):
        by_date = {f.target_date: f.lead_days for f in parse_run("KPHL", RUNTIME, REAL_ROWS)}
        assert by_date[dt.date(2025, 8, 2)] == 1
        assert by_date[dt.date(2025, 8, 5)] == 4

    def test_run_time_for_inverts_lead(self):
        assert run_time_for(dt.date(2025, 8, 5), 4) == RUNTIME

    def test_leads_beyond_the_horizon_are_dropped(self):
        rows = REAL_ROWS + [{"ftime": "2025-08-20 00:00", "n_x": 90}]
        assert all(f.lead_days <= MAX_LEAD_DAYS for f in parse_run("KPHL", RUNTIME, rows))


class TestRefusesPartialData:
    def test_empty_run_raises(self):
        with pytest.raises(MosUnavailable, match="no rows"):
            parse_run("KPHL", RUNTIME, [])

    def test_short_run_raises_rather_than_returning_the_good_half(self):
        with pytest.raises(MosUnavailable, match="usable leads"):
            parse_run("KPHL", RUNTIME, REAL_ROWS[:3])

    def test_null_filled_run_raises(self):
        """The Open-Meteo failure mode: 200 OK with nulls. Counted, then refused."""
        rows = [{"ftime": r["ftime"], "n_x": None} for r in REAL_ROWS]
        with pytest.raises(MosUnavailable, match="null values"):
            parse_run("KPHL", RUNTIME, rows)

    def test_implausible_value_raises(self):
        rows = list(REAL_ROWS)
        rows[1] = {"ftime": "2025-08-03 00:00", "n_x": 999}
        with pytest.raises(MosUnavailable, match="implausible"):
            parse_run("KPHL", RUNTIME, rows)

    def test_min_leads_is_actually_enforced(self):
        rows = [r for r in REAL_ROWS if r["ftime"].endswith("00:00")][: MIN_LEADS - 1]
        with pytest.raises(MosUnavailable):
            parse_run("KPHL", RUNTIME, rows)


class TestFetchTreatsFailureAsUnavailable:
    def test_http_error_status_raises(self):
        with pytest.raises(MosUnavailable, match="HTTP 500"):
            fetch_run("KPHL", RUNTIME, http=_Http({}, status=500))

    def test_iem_no_data_body_raises(self):
        """IEM answers 'no data' with HTTP 200 and a detail body — the exact
        silent-failure shape that must never become a price."""
        payload = {"detail": "Database query found no results"}
        with pytest.raises(MosUnavailable, match="no results"):
            fetch_run("KPHL", RUNTIME, http=_Http(payload))

    def test_network_failure_raises_rather_than_returning_empty(self):
        http = _Http({}, raises=OSError("connection reset"))
        with pytest.raises(MosUnavailable, match="request failed"):
            fetch_run("KPHL", RUNTIME, http=http)

    def test_successful_fetch_requests_the_right_run(self):
        http = _Http({"data": REAL_ROWS})
        out = fetch_run("KPHL", RUNTIME, http=http)
        assert http.calls[0]["station"] == "KPHL"
        assert http.calls[0]["runtime"] == "2025-08-01T12:00:00Z"
        assert len(out) >= MIN_LEADS


@pytest.mark.live
class TestAgainstLiveIem:
    def test_every_configured_station_has_usable_guidance(self):
        """If IEM stops serving a station, that station must go unpriceable —
        this is how we find out, rather than discovering it through a
        mysteriously silent model."""
        from src.trading_config import ingest_series_list
        from src.weather.stations import station_for_series

        runtime = run_time_for(dt.date.today() - dt.timedelta(days=2), 1)
        for series in ingest_series_list():
            station = station_for_series(series)
            assert station is not None, f"{series} has no station mapping"
            forecasts = fetch_run(station.mos_station, runtime)
            assert len(forecasts) >= MIN_LEADS
            assert all(-80 <= f.max_temp_f <= 140 for f in forecasts)
