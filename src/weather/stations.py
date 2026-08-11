"""Which station each temperature series settles on.

Read off the contract text, not guessed. Verbatim from `rules_primary`
(2026-08-11): "If the highest temperature recorded in Central Park, New York
for August 12, 2026 as reported by the National Weather Service's
Climatological Report (Daily), is greater than 90°, then the market resolves to
Yes."

Two traps this map exists to avoid:

  Chicago settles on MIDWAY, not O'Hare. Picking the obvious airport would
  misprice the entire Chicago book, and nothing in the ticker says which.

  Settlement is the CLI product — whole degrees, local calendar day, at one
  named station. A gridded reanalysis value for the same city is a different
  number: measured 2026-08-11, Open-Meteo's grid ran +1.50 °F mean and +3.3 °F
  max against the Central Park observation. Against 1-degree buckets that gap
  dominates every modelling refinement, so both training and scoring use the
  station series.

`ghcn_id` is the NCEI GHCN-Daily station, which carries the same official
observation the CLI reports, but as a deep archive rather than a two-week
window. It is the truth series for calibration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Station:
    series_ticker: str
    name: str          # as it appears in the contract rules
    cli_location: str  # NWS CLI product location code
    ghcn_id: str       # NCEI GHCN-Daily station id
    latitude: float
    longitude: float
    timezone: str      # settlement uses the LOCAL calendar day
    # A distinctive phrase that must appear in the live rules text. If Kalshi
    # ever repoints a series at a different station, the guard test fails
    # instead of the model silently pricing the wrong place.
    rules_marker: str


STATIONS: Dict[str, Station] = {
    "KXHIGHNY": Station(
        "KXHIGHNY", "Central Park, New York", "NYC", "USW00094728",
        40.7789, -73.9692, "America/New_York", "central park",
    ),
    "KXHIGHCHI": Station(
        # MIDWAY. Not O'Hare.
        "KXHIGHCHI", "Chicago Midway, IL", "MDW", "USW00014819",
        41.7860, -87.7524, "America/Chicago", "midway",
    ),
    "KXHIGHMIA": Station(
        "KXHIGHMIA", "Miami International Airport", "MIA", "USW00012839",
        25.7932, -80.2906, "America/New_York", "miami international",
    ),
    "KXHIGHDEN": Station(
        "KXHIGHDEN", "Denver, CO", "DEN", "USW00003017",
        39.8467, -104.6562, "America/Denver", "denver",
    ),
    "KXHIGHAUS": Station(
        "KXHIGHAUS", "Austin Bergstrom", "AUS", "USW00013904",
        30.1975, -97.6664, "America/Chicago", "austin bergstrom",
    ),
    "KXHIGHLAX": Station(
        "KXHIGHLAX", "Los Angeles Airport, CA", "LAX", "USW00023174",
        33.9381, -118.3889, "America/Los_Angeles", "los angeles airport",
    ),
    "KXHIGHPHIL": Station(
        "KXHIGHPHIL", "Philadelphia International Airport", "PHL", "USW00013739",
        39.8683, -75.2311, "America/New_York", "philadelphia international",
    ),
}


def station_for_series(series_ticker: str) -> Optional[Station]:
    return STATIONS.get(series_ticker)


def station_for_market(market_id: str) -> Optional[Station]:
    """Station for a market ticker like `KXHIGHNY-26AUG12-T90`.

    Returns None for anything unmapped — an unknown series is not priceable,
    and inferring a station from a city name in the title would reintroduce
    exactly the guessing this module exists to remove.
    """
    if not market_id:
        return None
    return STATIONS.get(market_id.split("-")[0])
