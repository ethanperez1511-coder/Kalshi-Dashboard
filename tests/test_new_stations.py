"""The twelve added temperature series, held to the mapping that verified them.

Verification was a settled-ladder reconstruction: implied settlement temperature
recovered from ~50 settled strike ladders per series and compared against GHCN,
13/14 at 100% bracket match, with KXHIGHNY 43/43 and KXHIGHCHI 55/55 as method
controls. Houston tested against IAH scored 14/54 versus Hobby's 49/54, which is
what proved the Hobby mapping rather than assuming the obvious airport.

These tests pin the parts of that mapping a future edit could silently break:
the airport traps, the timezone that defines the settlement day, and the two
deliberate exclusions.
"""
from __future__ import annotations

import pytest

from src.trading_config import ingest_series_list
from src.weather.settlement_guard import verify_settlement
from src.weather.stations import STATIONS, station_for_series

ADDED = [
    "KXHIGHTATL", "KXHIGHTBOS", "KXHIGHTDAL", "KXHIGHTDC", "KXHIGHTLV",
    "KXHIGHTMIN", "KXHIGHTNOLA", "KXHIGHTOKC", "KXHIGHTPHX", "KXHIGHTSATX",
    "KXHIGHTSEA", "KXHIGHTSFO",
]
ORIGINAL = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHLAX", "KXHIGHPHIL",
]


class TestAllTwelveAreMapped:
    @pytest.mark.parametrize("series", ADDED)
    def test_the_series_has_a_station(self, series):
        assert station_for_series(series) is not None

    def test_the_original_seven_are_untouched(self):
        for series in ORIGINAL:
            assert station_for_series(series) is not None

    def test_every_series_is_ingested(self):
        configured = set(ingest_series_list())
        for series in ADDED + ORIGINAL:
            assert series in configured, f"{series} is mapped but never fetched"


class TestTheAirportTraps:
    """Nothing in a ticker says which airport. Three of the twelve are traps of
    exactly the Chicago/Midway kind, and each was settled by reading the CLI
    product code rather than the city name."""

    @pytest.mark.parametrize("series,cli,ghcn", [
        ("KXHIGHTDAL", "CLIDFW", "USW00003927"),   # DFW, NOT Love Field
        ("KXHIGHTDC", "CLIDCA", "USW00013743"),    # National, NOT Dulles
        ("KXHIGHTMIN", "CLIMSP", "USW00014922"),
    ])
    def test_the_verified_site_is_the_one_stored(self, series, cli, ghcn):
        station = station_for_series(series)

        assert station.cli_marker == cli
        assert station.ghcn_id == ghcn

    def test_houston_is_absent_because_its_truth_series_diverges(self):
        """GHCN disagreed with the CLI product on 5 of 51 days by 2-4 F, and
        Kalshi settles on CLI. Fitting sigma against GHCN would calibrate on a
        series that disagrees with settlement ~10% of the time — the precise
        miscalibration the promotion gate exists to block."""
        assert station_for_series("KXHIGHTHOU") is None
        assert "KXHIGHTHOU" not in ingest_series_list()

    def test_san_diego_is_absent_because_there_is_no_sample(self):
        """Launched 2026-08-20: 24 settled rows, 1-day volume 38 contracts.
        The mapping verifies; there is nothing to validate it against."""
        assert station_for_series("KXHIGHTSAN") is None


class TestSettlementDayIsLocal:
    """Settlement is the LOCAL calendar day, so the timezone is not cosmetic."""

    @pytest.mark.parametrize("series,tz", [
        ("KXHIGHTSEA", "America/Los_Angeles"),
        ("KXHIGHTSFO", "America/Los_Angeles"),
        ("KXHIGHTLV", "America/Los_Angeles"),
        ("KXHIGHTBOS", "America/New_York"),
        ("KXHIGHTATL", "America/New_York"),
        ("KXHIGHTDC", "America/New_York"),
        ("KXHIGHTDAL", "America/Chicago"),
        ("KXHIGHTMIN", "America/Chicago"),
        ("KXHIGHTNOLA", "America/Chicago"),
        ("KXHIGHTOKC", "America/Chicago"),
        ("KXHIGHTSATX", "America/Chicago"),
    ])
    def test_timezone(self, series, tz):
        assert station_for_series(series).timezone == tz

    def test_phoenix_does_not_observe_daylight_saving(self):
        """Arizona stays on MST year-round. Filing Phoenix under
        America/Los_Angeles or America/Denver would shift its settlement day by
        an hour for half the year — silently, and only in summer."""
        assert station_for_series("KXHIGHTPHX").timezone == "America/Phoenix"


class TestIdentifiersAreDistinct:
    def test_no_two_series_share_a_ghcn_station(self):
        ids = [s.ghcn_id for s in STATIONS.values()]
        assert len(ids) == len(set(ids))

    def test_no_two_series_share_a_mos_station(self):
        ids = [s.mos_station for s in STATIONS.values()]
        assert len(ids) == len(set(ids))

    def test_no_two_series_share_a_cli_code(self):
        codes = [s.cli_marker for s in STATIONS.values()]
        assert len(codes) == len(set(codes))


class TestTheGuardAcceptsTheLiveWording:
    @pytest.mark.parametrize("series", ADDED)
    def test_current_rules_shape_verifies(self, series):
        """Verbatim shape pulled from the live API during the probe."""
        station = station_for_series(series)
        ticker = f"{series}-26AUG25-T90"
        rules = (
            f"If the maximum temperature recorded at {station.name} "
            f"({station.cli_marker}) for Aug 25, 2026, is greater than 90 "
            f"fahrenheit according to The Weather Company, then the market "
            f"resolves to Yes."
        )

        ok, reason = verify_settlement(ticker, rules)

        assert ok, reason

    @pytest.mark.parametrize("series", ADDED)
    def test_a_repointed_site_is_refused(self, series):
        ticker = f"{series}-26AUG25-T90"
        rules = (
            "If the maximum temperature recorded at Elsewhere (CLIZZZ) for "
            "Aug 25, 2026, is greater than 90 fahrenheit according to The "
            "Weather Company, then the market resolves to Yes."
        )

        ok, reason = verify_settlement(ticker, rules)

        assert not ok
        assert "site" in reason
