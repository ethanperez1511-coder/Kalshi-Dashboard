"""Fees come from Kalshi, per series, and a fee we cannot vouch for is a refusal.

Two hardcoded rates (0.07 in ev/calculator.py and in trading/fees.py) assumed
every series charges the standard quadratic taker fee and no maker fee. Kalshi
publishes the real schedule per series (GET /series/{s}: fee_type,
fee_multiplier; measured 2026-10-06 on KXHIGHNY: "quadratic", 1). Rulings
2026-10-06 (D):

  * read the schedule from the API; alert when it changes
  * fetch failed: use the last known schedule if under 24 h old, else refuse
    the series; the digest names every series on a cached fee; a refusal is a
    Telegram alert, not a log line
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.trading.fee_schedule import (
    BASE_MAKER_RATE,
    BASE_TAKER_RATE,
    FeeBook,
    FeeSchedule,
    format_fee_digest,
    fee_digest,
)

NOW = dt.datetime(2026, 10, 7, 15, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    return db_engine


def _book(engine, answers, now=NOW):
    """answers: series -> (fee_type, multiplier) | Exception."""
    alerts = []

    def fetch(series):
        answer = answers[series]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return FeeBook(engine, fetch=fetch, alert=alerts.append, now=now), alerts


class TestRates:
    def test_the_standard_schedule_is_exactly_todays_hardcoded_rate(self):
        """Pinned equality: adopting the API schedule changes no EV number
        today. If this fails, every trade's edge moved."""
        from src.trading_config import KALSHI_FEE_RATE

        s = FeeSchedule("KXHIGHNY", "quadratic", 1.0, NOW)
        assert s.taker_rate == BASE_TAKER_RATE == KALSHI_FEE_RATE == 0.07
        assert s.maker_rate == 0.0

    def test_maker_fees_only_where_the_series_says_so(self):
        s = FeeSchedule("X", "quadratic_with_maker_fees", 1.0, NOW)
        assert s.maker_rate == BASE_MAKER_RATE == 0.0175

    def test_the_multiplier_scales_both_sides(self):
        s = FeeSchedule("X", "quadratic_with_maker_fees", 2.0, NOW)
        assert s.taker_rate == pytest.approx(0.14) and s.maker_rate == pytest.approx(0.035)


class TestFeeBook:
    def test_a_live_fetch_is_used_and_remembered(self, engine):
        book, alerts = _book(engine, {"KXHIGHNY": ("quadratic", 1)})
        schedule, refusal = book.schedule("KXHIGHNY")
        assert refusal is None and not schedule.cached
        assert alerts == []

    def test_an_unmodelled_fee_type_is_refused_and_alerted(self, engine):
        book, alerts = _book(engine, {"KXFLAT": ("flat", 1)})
        schedule, refusal = book.schedule("KXFLAT")
        assert schedule is None and "flat" in refusal
        assert len(alerts) == 1

    def test_a_change_is_alerted_with_old_and_new(self, engine):
        _book(engine, {"KXHIGHNY": ("quadratic", 1)})[0].schedule("KXHIGHNY")
        later = NOW + dt.timedelta(hours=1)
        book, alerts = _book(engine, {"KXHIGHNY": ("quadratic_with_maker_fees", 1)}, now=later)

        schedule, _ = book.schedule("KXHIGHNY")

        assert schedule.fee_type == "quadratic_with_maker_fees"
        assert len(alerts) == 1 and "quadratic" in alerts[0] and "maker" in alerts[0]

    def test_a_failed_fetch_uses_a_schedule_under_24h_old(self, engine):
        _book(engine, {"KXHIGHNY": ("quadratic", 1)})[0].schedule("KXHIGHNY")
        later = NOW + dt.timedelta(hours=23)
        book, alerts = _book(engine, {"KXHIGHNY": RuntimeError("503")}, now=later)

        schedule, refusal = book.schedule("KXHIGHNY")

        assert refusal is None and schedule.cached
        assert alerts == []                      # named in the digest instead

    def test_a_failed_fetch_with_a_stale_schedule_is_refused_and_alerted(self, engine):
        _book(engine, {"KXHIGHNY": ("quadratic", 1)})[0].schedule("KXHIGHNY")
        later = NOW + dt.timedelta(hours=25)
        book, alerts = _book(engine, {"KXHIGHNY": RuntimeError("503")}, now=later)

        schedule, refusal = book.schedule("KXHIGHNY")

        assert schedule is None and "24" in refusal
        assert len(alerts) == 1

    def test_a_failed_fetch_with_nothing_known_is_refused(self, engine):
        book, alerts = _book(engine, {"KXNEW": RuntimeError("timeout")})
        schedule, refusal = book.schedule("KXNEW")
        assert schedule is None and refusal and len(alerts) == 1

    def test_one_alert_per_series_per_cycle(self, engine):
        book, alerts = _book(engine, {"KXNEW": RuntimeError("timeout")})
        book.schedule("KXNEW")
        book.schedule("KXNEW")
        assert len(alerts) == 1


class TestDigest:
    def test_the_digest_names_every_series_on_a_cached_fee(self, engine):
        _book(engine, {"KXHIGHNY": ("quadratic", 1), "KXHIGHCHI": ("quadratic", 1)})[0].schedule("KXHIGHNY")
        _book(engine, {"KXHIGHCHI": ("quadratic", 1)})[0].schedule("KXHIGHCHI")
        later = NOW + dt.timedelta(hours=5)
        _book(engine, {"KXHIGHNY": RuntimeError("503")}, now=later)[0].schedule("KXHIGHNY")

        line = format_fee_digest(fee_digest(engine, now=later))

        assert "CACHED" in line and "KXHIGHNY" in line
        assert "KXHIGHCHI" not in line.split("CACHED")[1]


def test_the_standard_schedule_charges_exactly_what_kalshi_fee_charges():
    """Every price, several sizes: the API-driven fee and the legacy function
    must agree to the cent on today's schedule."""
    from src.trading.fees import kalshi_fee

    s = FeeSchedule("KXHIGHNY", "quadratic", 1, NOW)
    for qty in (1, 3, 7, 25, 100):
        for price in range(0, 101):
            assert s.fee(qty, price) == kalshi_fee(qty, price), (qty, price)
