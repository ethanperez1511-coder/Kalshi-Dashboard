"""Weather contract terms are parsed, never assumed (Phase 2.0, 2026-08-11).

A daily temperature contract is only tradeable if we know *exactly* what it
asks. Two things decide that and neither may be guessed:

  direction — the same city lists both ">90°" and "<83°" contracts on the same
              day. Assuming one inverts the question, which is the Polymarket
              direction bug (Phase 1.3) in a new place.
  threshold — the number and its unit.

Kalshi publishes both as structured fields, so the parser reads those and uses
the human-readable subtitle as an independent cross-check. Anything it cannot
confirm is marked unpriceable rather than defaulted.

Fixtures are verbatim from the live API on 2026-08-11.

BOUNDARY, verified against real payloads: ticker `KXHIGHNY-26AUG12-T90` has
title ">90°", `strike_type=greater`, `floor_strike=90` and subtitle
"**91° or above**". YES therefore means T >= 91 — strictly greater than the
strike. Off by one here moves probability mass at the most likely threshold.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.kalshi.schemas import KalshiMarket
from src.weather.terms import (
    ContractTerms,
    is_temperature_market,
    parse_contract_terms,
)


def _market(**kw) -> KalshiMarket:
    base = dict(
        ticker="KXHIGHNY-26AUG12-T90",
        title="Will the **high temp in NYC** be >90° on Aug 12, 2026?",
        category="Climate and Weather",
        close_time=datetime(2026, 8, 13, 4, 59, tzinfo=timezone.utc),
        status="active",
        subtitle="91° or above",
        yes_sub_title="91° or above",
        strike_type="greater",
        floor_strike=90.0,
    )
    base.update(kw)
    return KalshiMarket(**base)


# --------------------------------------------------------------------------
# 1. Direction — both variants, in both cities
# --------------------------------------------------------------------------

class TestDirection:
    def test_nyc_greater_contract(self):
        """KXHIGHNY-26AUG12-T90, verbatim from the live API."""
        terms = parse_contract_terms(_market())
        assert terms is not None
        assert terms.direction == "above"
        assert terms.threshold == 90.0
        assert terms.unit == "F"

    def test_nyc_less_contract(self):
        """The SAME city also lists a '<' contract on the SAME day."""
        terms = parse_contract_terms(_market(
            ticker="KXHIGHNY-26AUG12-T83",
            title="Will the **high temp in NYC** be <83° on Aug 12, 2026?",
            subtitle="82° or below", yes_sub_title="82° or below",
            strike_type="less", floor_strike=None, cap_strike=83.0,
        ))
        assert terms is not None
        assert terms.direction == "below"
        assert terms.threshold == 83.0

    def test_austin_less_contract(self):
        terms = parse_contract_terms(_market(
            ticker="KXHIGHAUS-26AUG12-T99",
            title="Will the **high temp in Austin** be <99° on Aug 12, 2026?",
            subtitle="98° or below", yes_sub_title="98° or below",
            strike_type="less", floor_strike=None, cap_strike=99.0,
        ))
        assert terms is not None
        assert terms.direction == "below"
        assert terms.threshold == 99.0

    def test_austin_greater_contract(self):
        """Austin lists '>' contracts too — direction is per-contract, not
        per-city. A city-level assumption would misprice half the book."""
        terms = parse_contract_terms(_market(
            ticker="KXHIGHAUS-26AUG12-T106",
            title="Will the **high temp in Austin** be >106° on Aug 12, 2026?",
            subtitle="107° or above", yes_sub_title="107° or above",
            strike_type="greater", floor_strike=106.0,
        ))
        assert terms is not None
        assert terms.direction == "above"
        assert terms.threshold == 106.0


# --------------------------------------------------------------------------
# 2. Boundary semantics — the off-by-one that moves the most mass
# --------------------------------------------------------------------------

class TestBoundary:
    def test_above_is_strictly_greater(self):
        """floor_strike=90 with subtitle '91° or above' => YES iff T >= 91."""
        terms = parse_contract_terms(_market())
        assert terms.resolves_yes(91) is True
        assert terms.resolves_yes(90) is False   # the strike itself is NO
        assert terms.resolves_yes(89) is False

    def test_below_is_strictly_less(self):
        terms = parse_contract_terms(_market(
            title="Will the **high temp in NYC** be <83° on Aug 12, 2026?",
            subtitle="82° or below", yes_sub_title="82° or below",
            strike_type="less", floor_strike=None, cap_strike=83.0,
        ))
        assert terms.resolves_yes(82) is True
        assert terms.resolves_yes(83) is False   # the strike itself is NO
        assert terms.resolves_yes(84) is False


# --------------------------------------------------------------------------
# 3. Refuse rather than default
# --------------------------------------------------------------------------

class TestRefusal:
    def test_missing_strike_type_is_unpriceable(self):
        assert parse_contract_terms(_market(
            strike_type="", floor_strike=None, cap_strike=None,
            title="Will the high temp in NYC be warm on Aug 12, 2026?",
            subtitle="", yes_sub_title="",
        )) is None

    def test_unknown_strike_type_is_unpriceable(self):
        """'between' is a real Kalshi type we do not model yet. Refuse it —
        silently treating a range as a one-sided threshold is a fake price."""
        assert parse_contract_terms(_market(
            strike_type="between", floor_strike=85.0, cap_strike=90.0,
            subtitle="85° to 90°", yes_sub_title="85° to 90°",
        )) is None

    def test_missing_threshold_value_is_unpriceable(self):
        assert parse_contract_terms(_market(
            strike_type="greater", floor_strike=None, cap_strike=None,
        )) is None

    def test_subtitle_contradicting_the_strike_is_unpriceable(self):
        """The cross-check earns its keep: structured says 'greater than 90'
        but the human-readable text says 'or below'. One of them is wrong and
        we cannot tell which, so we do not price it."""
        assert parse_contract_terms(_market(
            strike_type="greater", floor_strike=90.0,
            subtitle="89° or below", yes_sub_title="89° or below",
        )) is None

    def test_subtitle_off_by_one_is_unpriceable(self):
        """floor 90 implies 'YES at 91 or above'. A subtitle saying 90 means
        the boundary convention is not what we think it is."""
        assert parse_contract_terms(_market(
            strike_type="greater", floor_strike=90.0,
            subtitle="90° or above", yes_sub_title="90° or above",
        )) is None

    def test_absent_subtitle_still_parses_from_structured_fields(self):
        """No cross-check available is not the same as a failed cross-check."""
        terms = parse_contract_terms(_market(subtitle="", yes_sub_title=""))
        assert terms is not None
        assert terms.direction == "above"


# --------------------------------------------------------------------------
# 4. Text fallback when Kalshi omits the structured fields
# --------------------------------------------------------------------------

class TestTextFallback:
    def test_parses_direction_and_threshold_from_title(self):
        terms = parse_contract_terms(_market(
            strike_type="", floor_strike=None, cap_strike=None,
            subtitle="", yes_sub_title="",
        ))
        assert terms is not None
        assert terms.direction == "above"
        assert terms.threshold == 90.0
        assert terms.source == "title"

    def test_structured_fields_win_over_text(self):
        terms = parse_contract_terms(_market())
        assert terms.source == "structured"

    def test_ambiguous_title_is_unpriceable(self):
        assert parse_contract_terms(_market(
            title="Will it be hot in NYC on Aug 12, 2026?",
            strike_type="", floor_strike=None, cap_strike=None,
            subtitle="", yes_sub_title="",
        )) is None


# --------------------------------------------------------------------------
# 5. Non-weather markets are simply not applicable
# --------------------------------------------------------------------------

def test_non_weather_market_returns_none():
    assert parse_contract_terms(_market(
        ticker="KXNEXTISRAELPM-45JAN01-GEIS",
        title="Will Gadi Eisenkot be the next Prime Minister of Israel?",
        category="Politics", strike_type="", floor_strike=None,
        subtitle="", yes_sub_title="",
    )) is None


# --------------------------------------------------------------------------
# 6. Classifier scope — the four "unreadable" contracts in production
# --------------------------------------------------------------------------

class TestClassifierScope:
    """The first production digest reported 4 unreadable threshold contracts.
    Investigating them, per the design rule, found two different causes and
    neither was a parser bug."""

    def _m(self, ticker, title):
        return _market(ticker=ticker, title=title, strike_type="",
                       floor_strike=None, cap_strike=None, subtitle="",
                       yes_sub_title="")

    @pytest.mark.parametrize("title", [
        "Will Juno Temple perform as Moneypenny in the next James Bond film?",
        "Will they attempt a reboot before 2027?",
        "Will contemporary art sales exceed $1bn?",
        "Will the temporary ban be lifted?",
    ])
    def test_words_containing_temp_are_not_temperature_markets(self, title):
        """`"temp" in title` matched all of these. A James Bond casting market
        reached the weather parser and was reported as an unreadable contract."""
        assert is_temperature_market(self._m("KXOTHER-X", title)) is False

    def test_real_temperature_markets_still_match(self):
        assert is_temperature_market(
            self._m("KXHIGHNY-26AUG12-T90", "Will the **high temp in NYC** be >90°?")
        ) is True
        assert is_temperature_market(
            self._m("KXTXURI-1", "Will the average daily minimum temperature fall below 20°?")
        ) is True

    def test_a_station_we_model_is_in_scope(self):
        from src.weather.terms import is_in_scope

        assert is_in_scope(self._m("KXHIGHNY-26AUG12-T90", "high temp")) is True

    def test_a_temperature_market_we_cannot_price_is_out_of_scope(self):
        """The three Texas contracts: average daily MINIMUM across two airports.
        Neither a station we map nor the statistic we fit — so out of scope,
        not unreadable. 'Unreadable' is an alarm the design says to chase, and
        a correct refusal wearing that label wastes the alarm."""
        from src.weather.terms import is_in_scope

        assert is_in_scope(
            self._m("KXTXURI-28DEC31-27JAN01",
                    "Will the average daily minimum temperature in Texas fall below 20°?")
        ) is False
