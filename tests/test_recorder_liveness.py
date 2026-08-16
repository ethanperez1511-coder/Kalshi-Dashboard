"""Recorded hours only count if a live market was on the other end.

The recorder spent an unknown number of its 244,270 messages / 52 hours taping
markets that had already settled — KXHIGH*-26AUG13 contracts still subscribed
on 2026-08-16. Those rows are not merely useless. They are counted by
`recorder_health` as coverage and by `day7.measure` as prints, so they pull the
day-7 validation date FORWARD, which is the direction that costs money: the
maker-fill rule would be declared validated on a sample that contains dead
books quoting nothing.

So liveness is measured per row, against the market's own close date, and the
coverage number the day-7 decision reads counts live rows only. The dead ones
are reported rather than dropped quietly — the size of that number is the
answer to "how much of the record was real".
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.recorder.health import recorder_health

NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(engine, market_id, close_at, category="Weather"):
    with get_session(engine) as session:
        session.add(Market(
            market_id=market_id, title="t", category=category,
            close_date=close_at, status="active",
        ))
        session.commit()


def _row(engine, ticker, received_at, msg_type="delta"):
    with get_session(engine) as session:
        session.add(OrderbookDeltaRaw(
            market_ticker=ticker, msg_type=msg_type, sid=1, seq=1,
            payload="{}", received_at=received_at,
        ))
        session.commit()


class TestLivenessSplit:
    def test_rows_recorded_after_close_are_counted_as_dead(self, engine):
        _market(engine, "KXHIGHCHI-26AUG13-T76", NOW - dt.timedelta(days=3))
        _row(engine, "KXHIGHCHI-26AUG13-T76", NOW - dt.timedelta(days=4))   # live
        _row(engine, "KXHIGHCHI-26AUG13-T76", NOW - dt.timedelta(hours=2))  # dead
        _row(engine, "KXHIGHCHI-26AUG13-T76", NOW - dt.timedelta(hours=1))  # dead

        health = recorder_health(engine, now=NOW)

        assert health["liveness"]["live"] == 1
        assert health["liveness"]["dead"] == 2

    def test_a_row_with_no_market_row_is_unattributed_not_live(self, engine):
        """Blank tickers and unknown markets cannot be claimed as coverage."""
        _row(engine, "", NOW - dt.timedelta(hours=1))
        _row(engine, "GHOST-1", NOW - dt.timedelta(hours=1))

        health = recorder_health(engine, now=NOW)

        assert health["liveness"]["live"] == 0
        assert health["liveness"]["unattributed"] == 2

    def test_the_three_buckets_account_for_every_row(self, engine):
        _market(engine, "LIVE-1", NOW + dt.timedelta(days=1))
        _market(engine, "DEAD-1", NOW - dt.timedelta(days=1))
        _row(engine, "LIVE-1", NOW - dt.timedelta(hours=1))
        _row(engine, "DEAD-1", NOW - dt.timedelta(hours=1))
        _row(engine, "GHOST-1", NOW - dt.timedelta(hours=1))

        health = recorder_health(engine, now=NOW)
        buckets = health["liveness"]

        assert (
            buckets["live"] + buckets["dead"] + buckets["unattributed"]
            == health["messages"]
        )


class TestCoverageCountsLiveHoursOnly:
    def test_dead_hours_do_not_advance_the_clock(self, engine):
        """The number that moves the day-7 date."""
        _market(engine, "DEAD-1", NOW - dt.timedelta(days=2))
        for hour in range(6):
            _row(engine, "DEAD-1", NOW - dt.timedelta(hours=hour))

        health = recorder_health(engine, now=NOW)

        assert health["per_category"].get("Weather", {}).get("hours", 0) == 0

    def test_live_hours_still_count(self, engine):
        _market(engine, "LIVE-1", NOW + dt.timedelta(days=1))
        for hour in range(1, 4):
            _row(engine, "LIVE-1", NOW - dt.timedelta(hours=hour))

        health = recorder_health(engine, now=NOW)

        assert health["per_category"]["Weather"]["hours"] == 3
        assert health["per_category"]["Weather"]["messages"] == 3

    def test_a_mixed_market_counts_only_its_live_hours(self, engine):
        """A market recorded across its own close: the hours before count, the
        hours after do not, and no market is written off wholesale."""
        close = NOW - dt.timedelta(hours=3)
        _market(engine, "MIXED-1", close)
        for hour in (6, 5, 4):
            _row(engine, "MIXED-1", NOW - dt.timedelta(hours=hour))
        for hour in (2, 1):
            _row(engine, "MIXED-1", NOW - dt.timedelta(hours=hour))

        health = recorder_health(engine, now=NOW)

        assert health["per_category"]["Weather"]["hours"] == 3
        assert health["liveness"]["dead"] == 2


class TestReporting:
    def test_the_report_names_the_dead_share(self, engine):
        from src.recorder.health import format_recorder_health

        _market(engine, "DEAD-1", NOW - dt.timedelta(days=1))
        _row(engine, "DEAD-1", NOW - dt.timedelta(hours=1))

        text = format_recorder_health(recorder_health(engine, now=NOW))

        assert "dead" in text.lower()

    def test_a_clean_record_does_not_cry_wolf(self, engine):
        from src.recorder.health import format_recorder_health

        _market(engine, "LIVE-1", NOW + dt.timedelta(days=1))
        _row(engine, "LIVE-1", NOW - dt.timedelta(hours=1))

        text = format_recorder_health(recorder_health(engine, now=NOW))

        assert "dead" not in text.lower()
