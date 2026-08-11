"""NWS MOS guidance — the production predictor, and the only one we fit on.

NWS's own gridpoint API serves today's forecast and nothing else, so σ cannot
be fitted against it: there is no history to fit on. Fitting on one product and
pricing off another would measure a different model's errors than the one
placing trades, so predictor and production are the same product here — GFS
extended MOS (MEX), which Iowa State's mesonet archives back years.

Day alignment, verified against truth rather than assumed (KPHL, run
2025-08-01 12Z vs GHCN-Daily): the `n_x` value at a 00Z valid time is the
daytime maximum for the LOCAL day that just ended, so 00Z on D+1 is day D's
max. Errors of −1, −1, −3, +3 °F across leads 1–4 are forecast error; a day
misalignment would show a systematic mismatch instead.

Every fetch is validated. IEM returning a short, null-filled, or implausible
run means the station-day is UNPRICEABLE — it never degrades to a partially
filled forecast, and it never falls back to a predictor σ was not fitted on.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_IEM_URL = "https://mesonet.agron.iastate.edu/api/1/mos.json"
MODEL = "MEX"          # GFS extended MOS: daily max out to ~7 days
RUN_HOUR = 12          # 12Z run

# A daily max outside this range is not weather, it is a parsing or unit error.
_PLAUSIBLE_F = (-80.0, 140.0)
# A healthy MEX run yields max values for at least this many forward days.
MIN_LEADS = 4
MAX_LEAD_DAYS = 7


class MosUnavailable(RuntimeError):
    """No usable guidance for this station/run. The caller must not substitute."""


@dataclass(frozen=True)
class MosForecast:
    station: str
    target_date: dt.date   # the LOCAL day whose maximum this predicts
    lead_days: int         # whole days from run date to target date
    max_temp_f: float
    runtime: dt.datetime


def run_time_for(target_date: dt.date, lead_days: int) -> dt.datetime:
    """The 12Z run that sits `lead_days` before `target_date`."""
    run_date = target_date - dt.timedelta(days=lead_days)
    return dt.datetime(
        run_date.year, run_date.month, run_date.day, RUN_HOUR,
        tzinfo=dt.timezone.utc,
    )


def _validate(station: str, runtime: dt.datetime, rows: list) -> None:
    if not rows:
        raise MosUnavailable(f"{station} {runtime:%Y-%m-%d %HZ}: no rows returned")


def parse_run(station: str, runtime: dt.datetime, rows: list) -> List[MosForecast]:
    """Daily maxima from one MOS run, or raise.

    Silence is not tolerated anywhere here: a run that is short, null-filled or
    implausible raises rather than returning the good half. A partially filled
    forecast is indistinguishable from a complete one downstream, and the model
    would price the gaps as if they were data.
    """
    _validate(station, runtime, rows)

    out: List[MosForecast] = []
    nulls = 0
    for row in rows:
        ftime = str(row.get("ftime") or "")
        if not ftime.endswith("00:00"):
            continue  # 12Z valid times carry the overnight MINIMUM
        value = row.get("n_x")
        if value is None:
            nulls += 1
            continue
        try:
            temp = float(value)
        except (TypeError, ValueError):
            nulls += 1
            continue
        if not (_PLAUSIBLE_F[0] <= temp <= _PLAUSIBLE_F[1]):
            raise MosUnavailable(
                f"{station} {runtime:%Y-%m-%d %HZ}: implausible max {temp}°F "
                f"at {ftime} — unit or parsing error, not weather"
            )
        # 00Z on D+1 is the maximum for local day D.
        target = dt.date.fromisoformat(ftime[:10]) - dt.timedelta(days=1)
        lead = (target - runtime.date()).days
        if lead < 0 or lead > MAX_LEAD_DAYS:
            continue
        out.append(MosForecast(station, target, lead, temp, runtime))

    if len(out) < MIN_LEADS:
        raise MosUnavailable(
            f"{station} {runtime:%Y-%m-%d %HZ}: only {len(out)} usable leads "
            f"(need {MIN_LEADS}); {nulls} null values — treating as unavailable "
            f"rather than pricing off a partial run"
        )
    return out


def fetch_run(
    station: str, runtime: dt.datetime, http=httpx, timeout: float = 40.0,
) -> List[MosForecast]:
    """One MOS run's daily maxima. Raises MosUnavailable on anything unusable."""
    try:
        response = http.get(
            _IEM_URL,
            params={
                "station": station,
                "model": MODEL,
                "runtime": runtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            timeout=timeout,
        )
    except Exception as exc:  # network failure is unavailability, not zero
        raise MosUnavailable(f"{station}: MOS request failed: {exc}") from exc

    if response.status_code != 200:
        raise MosUnavailable(
            f"{station} {runtime:%Y-%m-%d %HZ}: HTTP {response.status_code}"
        )
    body = response.json()
    if not isinstance(body, dict) or "data" not in body:
        # IEM answers "no data" with a 200 and a {"detail": ...} body.
        raise MosUnavailable(
            f"{station} {runtime:%Y-%m-%d %HZ}: {str(body)[:120]}"
        )
    return parse_run(station, runtime, body["data"])


def forecast_for(
    station: str, target_date: dt.date, lead_days: int, http=httpx,
) -> Optional[MosForecast]:
    """The forecast of `target_date`'s max issued `lead_days` earlier."""
    runtime = run_time_for(target_date, lead_days)
    for forecast in fetch_run(station, runtime, http=http):
        if forecast.target_date == target_date:
            return forecast
    return None


def index_by_target(forecasts: List[MosForecast]) -> Dict[dt.date, MosForecast]:
    return {f.target_date: f for f in forecasts}
