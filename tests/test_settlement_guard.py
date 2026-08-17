"""The settlement guard has to run where the pricing happens.

Kalshi repointed all seven temperature series from the NWS Climatological
Report to The Weather Company on 2026-08-14. The guard that existed caught it —
and it was a `@pytest.mark.live` test in CI, so what it produced was a red
scheduled job three days running, while the pricing path went on knowing
nothing. Weather did not refuse because of the change; it happened to be dark
for an unrelated reason (the MOS blackout). Had it not been, this system would
have priced seven cities against a settlement authority nobody had checked.

A guard that only runs in CI gates CI. So the check now runs against the rules
text stored on the market row, every cycle, and a market whose settlement
description we cannot recognise is refused with a named reason.

Two things are verified, and they fail differently on purpose:

  the observing SITE, via the CLI product code — CLINYC, CLIMDW, CLIMIA,
  CLIDEN, CLIAUS, CLILAX, CLIPHL. Vendor-independent: it survived the switch
  from NWS to The Weather Company precisely because it names the station
  rather than the distributor.

  the settlement AUTHORITY, against a list of ones known to carry the CLI
  number. Measured 2026-08-14/15: TWC's published values matched the NWS CLI
  products 14/14 across all seven cities. A new name appearing there is not
  something to price through.
"""
from __future__ import annotations

import pytest

from src.weather.settlement_guard import (
    ACCEPTED_AUTHORITIES,
    verify_settlement,
)
from src.weather.stations import STATIONS, station_for_market

NY = "KXHIGHNY-26AUG17-T91"

CURRENT = (
    "If the maximum temperature recorded at New York City (CLINYC) for "
    "Aug 17, 2026, is greater than 91° fahrenheit according to The Weather "
    "Company, then the market resolves to Yes."
)
PREVIOUS = (
    "If the highest temperature recorded in Central Park, New York for "
    "August 12, 2026 as reported by the National Weather Service's "
    "Climatological Report (Daily), is greater than 90°, then the market "
    "resolves to Yes."
)


class TestBothErasPass:
    def test_the_current_twc_wording_verifies(self):
        ok, reason = verify_settlement(NY, CURRENT)
        assert ok, reason

    def test_the_previous_nws_wording_still_verifies(self):
        """The switch was a distributor change, not a number change — 14/14
        exact against the CLI products. Refusing the old wording would be
        refusing a contract that settles identically."""
        ok, reason = verify_settlement(NY, PREVIOUS)
        assert ok, reason


class TestSiteChanges:
    def test_a_different_cli_code_is_refused(self):
        """The trap the map exists for: Chicago settles on Midway, not O'Hare.
        A repoint like that is invisible in the ticker."""
        text = CURRENT.replace("CLINYC", "CLILGA")

        ok, reason = verify_settlement(NY, text)

        assert not ok
        assert "site" in reason

    def test_the_chicago_code_is_midway(self):
        assert station_for_market("KXHIGHCHI-26AUG17-T85").cli_marker == "CLIMDW"

    def test_every_station_has_a_distinct_marker(self):
        markers = [s.cli_marker for s in STATIONS.values()]
        assert len(markers) == len(set(markers))


class TestAuthorityChanges:
    def test_an_unknown_settlement_authority_is_refused(self):
        text = CURRENT.replace("The Weather Company", "Acme Weather Syndicate")

        ok, reason = verify_settlement(NY, text)

        assert not ok
        assert "authority" in reason

    def test_the_accepted_list_is_the_ones_measured_to_carry_the_cli_number(self):
        assert "the weather company" in ACCEPTED_AUTHORITIES
        assert "national weather service" in ACCEPTED_AUTHORITIES


class TestUnverifiable:
    @pytest.mark.parametrize("text", ["", None, "   "])
    def test_missing_rules_text_is_refused_not_assumed(self, text):
        """Absent evidence is not evidence. This is the whole design rule."""
        ok, reason = verify_settlement(NY, text)

        assert not ok
        assert "missing" in reason

    def test_a_market_with_no_station_is_refused(self):
        ok, reason = verify_settlement("KXHIGHBOISE-26AUG17-T90", CURRENT)

        assert not ok


class TestCaseAndFormatting:
    def test_matching_is_case_insensitive(self):
        ok, _ = verify_settlement(NY, CURRENT.lower())
        assert ok

    def test_the_code_is_matched_as_a_token_not_a_substring(self):
        """CLINYC must not be satisfied by CLINYCX — a new product code for a
        different site would otherwise pass silently."""
        text = CURRENT.replace("(CLINYC)", "(CLINYCX)")

        ok, _ = verify_settlement(NY, text)

        assert not ok
