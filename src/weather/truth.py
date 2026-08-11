"""Realized daily maxima — the values contracts actually settle on.

Kalshi settles on the NWS Climatological Report (Daily) at a named station.
NCEI's GHCN-Daily TMAX is that same official observation, published as a deep
archive rather than the CLI feed's two-week window, in whole degrees Fahrenheit
— the same units and rounding the contract uses.

Deliberately NOT a gridded reanalysis. Measured 2026-08-11, a grid value for
"New York" ran +1.50 °F mean and +3.3 °F max against the Central Park
observation; against 1-degree buckets that gap alone would dominate every
modelling refinement.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict

import httpx

logger = logging.getLogger(__name__)

_NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
_PLAUSIBLE_F = (-80.0, 140.0)


class TruthUnavailable(RuntimeError):
    """No realized observation. Never substitute an estimate for one."""


def _fetch_via_curl(ghcn_id: str, params: dict, timeout: float):
    """Last-resort fetch for hosts where httpx stalls on NCEI."""
    import json
    import subprocess
    import urllib.parse

    url = f"{_NCEI_URL}?{urllib.parse.urlencode(params)}"
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(int(timeout)),
             "-H", "User-Agent: kalshi-trading-bot (research)", url],
            capture_output=True, text=True, timeout=timeout + 15,
        )
    except Exception as exc:
        raise TruthUnavailable(f"{ghcn_id}: NCEI curl fallback failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise TruthUnavailable(f"{ghcn_id}: NCEI curl fallback returned nothing")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TruthUnavailable(f"{ghcn_id}: NCEI curl fallback non-JSON") from exc


def fetch_daily_max(
    ghcn_id: str,
    start: dt.date,
    end: dt.date,
    http=httpx,
    timeout: float = 90.0,
) -> Dict[dt.date, float]:
    """{date: observed daily max °F} for a station, or raise.

    Missing days are simply absent from the mapping — station records do have
    genuine gaps, and inventing a value for one would be fabricating the
    outcome a model is scored against.
    """
    params = {
        "dataset": "daily-summaries",
        "stations": ghcn_id,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dataTypes": "TMAX",
        "units": "standard",
        "format": "json",
    }

    rows = None
    try:
        response = http.get(
            _NCEI_URL, params=params,
            headers={"User-Agent": "kalshi-trading-bot (research)"},
            timeout=timeout,
        )
        if response.status_code != 200:
            raise TruthUnavailable(f"{ghcn_id}: NCEI HTTP {response.status_code}")
        rows = response.json()
    except TruthUnavailable:
        raise
    except Exception as exc:
        # Observed repeatedly: httpx hangs against this endpoint on some hosts
        # where curl returns the same full range in under two seconds. Rather
        # than let a client-library quirk decide whether the model has a truth
        # series, fall back and say so.
        logger.warning(
            "NCEI via httpx failed for %s (%s) — retrying with curl", ghcn_id, exc,
        )
        rows = _fetch_via_curl(ghcn_id, params, timeout)
    if not isinstance(rows, list):
        raise TruthUnavailable(f"{ghcn_id}: unexpected NCEI payload {str(rows)[:120]}")

    out: Dict[dt.date, float] = {}
    for row in rows:
        raw = row.get("TMAX")
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not (_PLAUSIBLE_F[0] <= value <= _PLAUSIBLE_F[1]):
            raise TruthUnavailable(
                f"{ghcn_id}: implausible observed max {value}°F on {row.get('DATE')}"
            )
        out[dt.date.fromisoformat(row["DATE"])] = value

    if not out:
        raise TruthUnavailable(
            f"{ghcn_id}: no observations for {start}..{end} — refusing to score "
            f"against an empty truth series"
        )
    return out
