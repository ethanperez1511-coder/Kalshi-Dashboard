"""Never persist a market no model will ever price.

Measured 2026-08-17: 374,000 of 376,000 "open" markets were
KXMVECROSSCATEGORY (218k) and KXMVESPORTSMULTIGAMEEXTENDED (156k) — parlay
combinations Kalshi mints continuously, 123,000 new rows on 2026-08-15 alone.
The scorer reached 2,270 of them. At ~60 MB/day against 126 MB of headroom the
free tier had about two days left.

Retention cannot fix this and neither can archival: the rows are not history,
they are a firehose. The write has to not happen. Filtering at ingest kills the
market row and its price snapshot together, which is where the bytes are.

Exclusions are configuration, not a constant, and they are COUNTED. An
invisible filter is how a legitimate series gets dropped for a month without
anyone noticing — the funnel carries the exclusion counts every cycle for the
same reason it carries every other rejection reason.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.ingestion.exclusions import (
    concentration_warnings,
    filter_ingestable,
    is_excluded_series,
    series_of,
)


class _Market:
    def __init__(self, ticker, mve_collection_ticker=None):
        self.ticker = ticker
        self.mve_collection_ticker = mve_collection_ticker


class TestSeriesExtraction:
    def test_series_is_the_ticker_prefix(self):
        assert series_of("KXMVECROSSCATEGORY-26AUG17-ABC") == "KXMVECROSSCATEGORY"
        assert series_of("KXHIGHNY-26AUG17-T91") == "KXHIGHNY"

    def test_a_ticker_with_no_separator_is_its_own_series(self):
        assert series_of("WEIRD") == "WEIRD"

    def test_blank_is_not_a_series(self):
        assert series_of("") == ""
        assert series_of(None) == ""


class TestExclusion:
    def test_the_two_measured_offenders_are_excluded_by_default(self):
        assert is_excluded_series("KXMVECROSSCATEGORY-26AUG17-ABC")
        assert is_excluded_series("KXMVESPORTSMULTIGAMEEXTENDED-S2026-YYY")

    def test_weather_is_never_excluded(self):
        for series in (
            "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHDEN",
            "KXHIGHAUS", "KXHIGHLAX", "KXHIGHPHIL",
        ):
            assert not is_excluded_series(f"{series}-26AUG17-T90")

    def test_the_list_is_configuration(self, monkeypatch):
        import src.ingestion.exclusions as mod

        monkeypatch.setattr(mod, "EXCLUDED_SERIES", frozenset({"KXSPAM"}))

        assert is_excluded_series("KXSPAM-1-2")
        assert not is_excluded_series("KXMVECROSSCATEGORY-1-2")

    def test_matching_is_case_insensitive_and_exact_on_the_series(self):
        """A prefix match would take KXHIGHNY out with KXHIGH."""
        assert is_excluded_series("kxmvecrosscategory-1-2")
        assert not is_excluded_series("KXMVECROSSCATEGORYEXTRA-1-2")


class TestFiltering:
    def test_excluded_markets_are_dropped_and_counted(self):
        markets = [
            _Market("KXHIGHNY-26AUG17-T91"),
            _Market("KXMVECROSSCATEGORY-26AUG17-A"),
            _Market("KXMVECROSSCATEGORY-26AUG17-B"),
            _Market("KXMVESPORTSMULTIGAMEEXTENDED-S1-A"),
        ]
        counts = {}

        kept = filter_ingestable(markets, counts)

        assert [m.ticker for m in kept] == ["KXHIGHNY-26AUG17-T91"]
        assert counts["KXMVECROSSCATEGORY"] == 2
        assert counts["KXMVESPORTSMULTIGAMEEXTENDED"] == 1

    def test_nothing_excluded_leaves_the_list_untouched(self):
        markets = [_Market("KXHIGHNY-26AUG17-T91"), _Market("KXHIGHLAX-26AUG17-T83")]
        counts = {}

        assert filter_ingestable(markets, counts) == markets
        assert counts == {}

    def test_filtering_is_safe_with_no_counter(self):
        assert filter_ingestable([_Market("KXMVECROSSCATEGORY-1-2")]) == []


class TestConcentrationDetector:
    """The generic answer to the next spam source, which will not be these two.

    An exclusion list only knows about yesterday's firehose. A series that
    suddenly dominates a fetch is the shape of the problem itself, and it is
    worth a number in the log before it is worth an emergency.
    """

    def test_a_dominant_series_is_reported(self):
        markets = [_Market(f"KXFLOOD-{i}") for i in range(90)]
        markets += [_Market(f"KXHIGHNY-{i}") for i in range(10)]

        warnings = concentration_warnings(markets, threshold=0.25)

        assert warnings and warnings[0][0] == "KXFLOOD"
        assert warnings[0][1] == 90
        assert 0.89 < warnings[0][2] < 0.91

    def test_a_balanced_fetch_warns_about_nothing(self):
        markets = [_Market(f"KX{i % 10}-{i}") for i in range(100)]

        assert concentration_warnings(markets, threshold=0.25) == []

    def test_an_empty_fetch_is_not_a_warning(self):
        assert concentration_warnings([], threshold=0.25) == []

    def test_an_already_excluded_series_does_not_warn(self):
        """It is already handled; repeating it every cycle is noise that would
        train the operator to ignore the line that matters."""
        markets = [_Market(f"KXMVECROSSCATEGORY-{i}") for i in range(90)]
        markets += [_Market(f"KX{i % 10}-{i}") for i in range(100)]

        assert concentration_warnings(markets, threshold=0.25) == []


class TestCollectionSiblings:
    """2026-10-06: KXMVECROSSCATEGORY0 markets reached the database although
    KXMVECROSSCATEGORY is excluded. Kalshi minted a sibling series name into
    the SAME parlay collection: every one carries
    mve_collection_ticker='KXMVECROSSCATEGORY-R' (measured on the live API).
    The rule follows the collection Kalshi itself declares, not the name, and
    still never matches a prefix."""

    SIBLING = "KXMVECROSSCATEGORY0-S20268201371F47A-E979B8FB158"

    def test_a_sibling_series_in_an_excluded_collection_is_excluded(self):
        counts = {}
        kept = filter_ingestable(
            [_Market(self.SIBLING, "KXMVECROSSCATEGORY-R"), _Market("KXHIGHNY-26OCT07-T70")],
            counts,
        )
        assert [m.ticker for m in kept] == ["KXHIGHNY-26OCT07-T70"]
        # Tallied under its own name, so the digest shows the sibling exists.
        assert counts == {"KXMVECROSSCATEGORY0": 1}

    def test_a_parlay_in_a_collection_nobody_excluded_is_kept(self):
        """SportsOdds prices MVE parlays. Excluding every KXMVE market is a
        ruling about that model's universe, not an ingest fix."""
        market = _Market("KXMVESPORTSFOO-S2026-ABC", "KXMVESPORTSFOO-R")
        assert filter_ingestable([market]) == [market]

    def test_the_schema_keeps_kalshis_collection_field(self):
        from src.kalshi.schemas import KalshiMarket

        market = KalshiMarket(
            ticker=self.SIBLING, title="t", close_time="2026-10-07T00:00:00Z",
            status="active", mve_collection_ticker="KXMVECROSSCATEGORY-R",
        )
        assert market.mve_collection_ticker == "KXMVECROSSCATEGORY-R"

    def test_a_null_collection_is_no_collection(self):
        from src.kalshi.schemas import KalshiMarket

        market = KalshiMarket(
            ticker="KXHIGHNY-26OCT07-T70", title="t",
            close_time="2026-10-07T00:00:00Z", status="active",
            mve_collection_ticker=None,
        )
        assert filter_ingestable([market]) == [market]


class TestDetectorDenominator:
    def test_excluded_rows_do_not_dilute_a_new_mint(self):
        """The fetch is mostly excluded parlays. A new mint at 150 of 1,050
        rows read as 14% and never warned. It is 150 of the 450 rows that
        will actually be written: 33%."""
        markets = [_Market(f"KXMVECROSSCATEGORY-{i}") for i in range(600)]
        markets += [_Market(f"KXNEWMINT-{i}") for i in range(150)]
        markets += [_Market(f"KX{i % 30}-{i}") for i in range(300)]

        warnings = concentration_warnings(markets, threshold=0.25)

        assert [w[0] for w in warnings] == ["KXNEWMINT"]
        assert 0.33 < warnings[0][2] < 0.34
