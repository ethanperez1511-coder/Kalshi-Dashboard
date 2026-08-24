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

`mos_station` is the ICAO whose NWS MOS guidance is the production
predictor. Central Park turned out to carry MEX MOS under its own KNYC
identifier, so every series predicts from its own settlement site and no
cross-site offset term is needed anywhere.
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
    mos_station: str   # ICAO for NWS MOS guidance (the production predictor)
    latitude: float
    longitude: float
    timezone: str      # settlement uses the LOCAL calendar day
    # A distinctive phrase that appeared in the pre-2026-08-14 rules text.
    # Retained for messages only — it is NOT what the guard asserts any more.
    # Kalshi's TWC rewording dropped the site names ("Central Park", "Midway")
    # entirely, so a guard keyed on them would have failed on six of seven
    # series for a wording change while missing an actual repoint.
    rules_marker: str

    @property
    def cli_marker(self) -> str:
        """The token the live rules text must contain: CLINYC, CLIMDW, ...

        Derived from `cli_location` rather than stored, so the marker and the
        station it identifies cannot drift apart. Vendor-independent by
        construction, which is why it survived the NWS -> The Weather Company
        switch: it names the observing station, not the distributor.
        """
        return f"CLI{self.cli_location}"


STATIONS: Dict[str, Station] = {
    "KXHIGHNY": Station(
        "KXHIGHNY", "Central Park, New York", "NYC", "USW00094728",
        "KNYC", 40.7789, -73.9692, "America/New_York", "central park",
    ),
    "KXHIGHCHI": Station(
        # MIDWAY. Not O'Hare.
        "KXHIGHCHI", "Chicago Midway, IL", "MDW", "USW00014819",
        "KMDW", 41.7860, -87.7524, "America/Chicago", "midway",
    ),
    "KXHIGHMIA": Station(
        "KXHIGHMIA", "Miami International Airport", "MIA", "USW00012839",
        "KMIA", 25.7932, -80.2906, "America/New_York", "miami international",
    ),
    "KXHIGHDEN": Station(
        "KXHIGHDEN", "Denver, CO", "DEN", "USW00003017",
        "KDEN", 39.8467, -104.6562, "America/Denver", "denver",
    ),
    "KXHIGHAUS": Station(
        "KXHIGHAUS", "Austin Bergstrom", "AUS", "USW00013904",
        "KAUS", 30.1975, -97.6664, "America/Chicago", "austin bergstrom",
    ),
    "KXHIGHLAX": Station(
        "KXHIGHLAX", "Los Angeles Airport, CA", "LAX", "USW00023174",
        "KLAX", 33.9381, -118.3889, "America/Los_Angeles", "los angeles airport",
    ),
    "KXHIGHPHIL": Station(
        "KXHIGHPHIL", "Philadelphia International Airport", "PHL", "USW00013739",
        "KPHL", 39.8683, -75.2311, "America/New_York", "philadelphia international",
    ),
    # ---- Added 2026-08-24 -------------------------------------------------
    # Twelve series verified by settled-ladder reconstruction: the implied
    # settlement temperature was recovered from ~50 settled strike ladders per
    # series and compared against GHCN. 12/12 matched at 100%, with KXHIGHNY
    # (43/43) and KXHIGHCHI (55/55) run as method controls. Every CLI code was
    # confirmed against IEM's CLI product database, which returns the AWIPS id
    # and NWS station name per ICAO — a one-to-one proof, not an inference.
    #
    # Three are airport traps of exactly the Chicago/Midway kind, and the city
    # name would have picked the wrong site in each: Dallas settles on DFW and
    # not Love Field, Washington on National and not Dulles, and Houston — held
    # back for a different reason below — on Hobby and not Bush.
    #
    # NOT ADDED, deliberately:
    #   KXHIGHTHOU (Houston/Hobby)  GHCN disagreed with the CLI product on 5 of
    #       51 days by 2-4 F and Kalshi settles on CLI, so a fit against GHCN
    #       would be calibrated on a series that disagrees with settlement
    #       about 10% of the time. Needs a CLI truth feed first.
    #   KXHIGHTSAN (San Diego)      launched 2026-08-20; 24 settled rows and a
    #       1-day volume of 38 contracts. The mapping verifies and there is
    #       nothing to validate it against.
    "KXHIGHTATL": Station(
        "KXHIGHTATL", "Atlanta", "ATL", "USW00013874",
        "KATL", 33.6367, -84.4281, "America/New_York", "atlanta",
    ),
    "KXHIGHTBOS": Station(
        "KXHIGHTBOS", "Boston", "BOS", "USW00014739",
        "KBOS", 42.3606, -71.0097, "America/New_York", "boston",
    ),
    "KXHIGHTDAL": Station(
        # DALLAS/FORT WORTH. Not Love Field.
        "KXHIGHTDAL", "Dallas", "DFW", "USW00003927",
        "KDFW", 32.8998, -97.0403, "America/Chicago", "dallas",
    ),
    "KXHIGHTDC": Station(
        # WASHINGTON NATIONAL. Not Dulles.
        "KXHIGHTDC", "Washington DC", "DCA", "USW00013743",
        "KDCA", 38.8512, -77.0402, "America/New_York", "washington",
    ),
    "KXHIGHTLV": Station(
        "KXHIGHTLV", "Las Vegas", "LAS", "USW00023169",
        "KLAS", 36.0840, -115.1537, "America/Los_Angeles", "las vegas",
    ),
    "KXHIGHTMIN": Station(
        "KXHIGHTMIN", "Minneapolis", "MSP", "USW00014922",
        "KMSP", 44.8848, -93.2223, "America/Chicago", "minneapolis",
    ),
    "KXHIGHTNOLA": Station(
        "KXHIGHTNOLA", "New Orleans", "MSY", "USW00012916",
        "KMSY", 29.9934, -90.2581, "America/Chicago", "new orleans",
    ),
    "KXHIGHTOKC": Station(
        "KXHIGHTOKC", "Oklahoma City", "OKC", "USW00013967",
        "KOKC", 35.3931, -97.6007, "America/Chicago", "oklahoma city",
    ),
    "KXHIGHTPHX": Station(
        # America/Phoenix, NOT America/Denver or America/Los_Angeles: Arizona
        # does not observe daylight saving, so either of those would shift the
        # settlement day by an hour for half the year — silently, and only in
        # summer, which is when these contracts matter most.
        "KXHIGHTPHX", "Phoenix", "PHX", "USW00023183",
        "KPHX", 33.4343, -112.0116, "America/Phoenix", "phoenix",
    ),
    "KXHIGHTSATX": Station(
        "KXHIGHTSATX", "San Antonio", "SAT", "USW00012921",
        "KSAT", 29.5337, -98.4698, "America/Chicago", "san antonio",
    ),
    "KXHIGHTSEA": Station(
        "KXHIGHTSEA", "Seattle", "SEA", "USW00024233",
        "KSEA", 47.4502, -122.3088, "America/Los_Angeles", "seattle",
    ),
    "KXHIGHTSFO": Station(
        "KXHIGHTSFO", "San Francisco", "SFO", "USW00023234",
        "KSFO", 37.6213, -122.3790, "America/Los_Angeles", "san francisco",
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
