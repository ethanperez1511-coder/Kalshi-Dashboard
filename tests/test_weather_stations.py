"""Settlement-station mapping (Phase 2.1, 2026-08-11).

Kalshi settles each temperature series on the NWS Climatological Report (Daily)
at one named station. Getting that wrong is invisible: the model produces
confident, well-formed prices for the wrong place.

The live-API test at the bottom is the important one — it asserts the contract
text still names the station we mapped, so a Kalshi repointing breaks a test
instead of quietly mispricing a whole city.
"""
from __future__ import annotations



import pytest

from src.weather.stations import STATIONS, station_for_market, station_for_series


class TestMapping:
    def test_every_configured_series_has_a_station(self):
        from src.trading_config import ingest_series_list

        for ticker in ingest_series_list():
            assert station_for_series(ticker) is not None, (
                f"{ticker} is ingested but has no settlement station — it would "
                f"be scored against nothing"
            )

    def test_chicago_settles_on_midway_not_ohare(self):
        """The trap: nothing in the ticker says which airport, and O'Hare is
        the obvious guess. Verbatim rules text says 'Chicago Midway, IL'."""
        chi = station_for_series("KXHIGHCHI")
        assert "midway" in chi.name.lower()
        assert chi.ghcn_id == "USW00014819"      # KMDW, not KORD (USW00094846)

    def test_station_resolves_from_a_full_market_ticker(self):
        station = station_for_market("KXHIGHNY-26AUG12-T90")
        assert station is not None
        assert station.cli_location == "NYC"

    def test_unknown_series_is_none_not_a_guess(self):
        assert station_for_market("KXHIGHBOISE-26AUG12-T90") is None
        assert station_for_market("") is None
        assert station_for_series("nonsense") is None

    def test_timezones_are_local_to_the_station(self):
        """Settlement is the LOCAL calendar day, so a UTC-day aggregation would
        straddle the wrong 24 hours."""
        assert station_for_series("KXHIGHLAX").timezone == "America/Los_Angeles"
        assert station_for_series("KXHIGHDEN").timezone == "America/Denver"
        assert station_for_series("KXHIGHNY").timezone == "America/New_York"

    def test_ghcn_ids_are_distinct(self):
        ids = [s.ghcn_id for s in STATIONS.values()]
        assert len(ids) == len(set(ids))


def _offline() -> bool:
    """Credentials live in .env, not the environment, so ask Settings.

    Checking os.environ here silently skipped the guard on a machine that could
    in fact reach the API — a guard that never runs is not a guard.
    """
    try:
        from src.config import Settings

        return Settings().is_offline_mode
    except Exception:
        return True


@pytest.mark.skipif(_offline(), reason="needs live Kalshi credentials")
class TestAgainstLiveContracts:
    """Guard: the mapping must keep matching what the contracts actually say."""

    @pytest.mark.asyncio
    async def test_rules_text_still_names_the_mapped_station(self):
        from src.config import Settings
        from src.kalshi.client import KalshiClient

        client = KalshiClient.from_settings(Settings())
        try:
            for ticker, station in STATIONS.items():
                markets = await client.get_series_markets(ticker, max_markets=1)
                if not markets:
                    pytest.skip(f"{ticker} has no open markets right now")
                rules = (markets[0].rules_primary or "").lower()
                assert station.rules_marker in rules, (
                    f"{ticker} rules no longer mention {station.rules_marker!r} — "
                    f"Kalshi may have repointed the series. Rules: {rules[:200]}"
                )
                assert "climatological report" in rules
        finally:
            await client._http.aclose()
