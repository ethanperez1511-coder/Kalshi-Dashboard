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


@pytest.mark.live
class TestAgainstLiveContracts:
    """Guard: the mapping must keep matching what the contracts actually say.

    Marked `live` rather than skipped-if-credentials-missing. The earlier
    version used `skipif`, which meant that on any machine without creds in the
    environment the guard reported success while doing nothing — the failure
    mode a guard exists to prevent. Now the default suite deselects it by
    marker, the scheduled live-checks workflow runs it WITH credentials, and
    missing credentials there are a hard failure rather than a skip.
    """

    @pytest.mark.asyncio
    async def test_rules_text_still_verifies_for_every_series(self):
        """All seven, reported together.

        The previous version asserted inside the loop, so it aborted on
        KXHIGHNY and never revealed that Kalshi had repointed ALL SEVEN series
        on 2026-08-14. Three days of red runs undercounted the blast radius by
        7x. A guard that stops at the first failure is measuring one series.

        It also asserted the site NAME and the literal phrase "climatological
        report", both of which the TWC rewording dropped while settling on the
        same number. The runtime guard is the single source of truth for what
        counts as verified, so this exercises exactly that — CI and production
        cannot drift apart into two different definitions.
        """
        from src.config import Settings
        from src.kalshi.client import KalshiClient
        from src.weather.settlement_guard import verify_settlement

        settings = Settings()
        assert not settings.is_offline_mode, (
            "live suite ran without Kalshi credentials — this is a FAILURE, not "
            "a skip: the station guard cannot verify anything without them"
        )
        client = KalshiClient.from_settings(settings)
        failures = []
        checked = 0
        try:
            for ticker in STATIONS:
                markets = await client.get_series_markets(ticker, max_markets=1)
                if not markets:
                    failures.append(f"{ticker}: no open markets to check")
                    continue
                checked += 1
                market = markets[0]
                ok, reason = verify_settlement(
                    market.ticker, market.rules_primary or "",
                )
                if not ok:
                    failures.append(
                        f"{ticker}: {reason} — rules: "
                        f"{(market.rules_primary or '')[:180]}"
                    )
        finally:
            await client._http.aclose()

        assert checked, "no series had an open market — nothing was verified"
        assert not failures, (
            f"{len(failures)} of {len(STATIONS)} series failed settlement "
            "verification:\n  " + "\n  ".join(failures)
        )
