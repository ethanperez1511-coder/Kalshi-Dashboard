"""Retention, and the write-rate fix it depends on.

Measured: ingest writes ~5,084 snapshots per 5-minute cycle = 1.46M rows/day,
which at ~436 bytes/row fills Neon's 0.5 GB tier in 2.8 days. Retention alone
cannot fix a write rate that high, so both halves are tested — and the one
dataset that cannot be re-collected is tested to be refused.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.ingestion.price_recorder import record_price_snapshot
from src.maintenance.retention import (
    DELTA_VALIDATION_DAYS,
    SNAPSHOT_MAX_DAYS,
    apply_retention,
    database_size_bytes,
    delta_window_end,
    format_size_line,
    plan_retention,
)
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.models.price import PriceSnapshot

NOW = dt.datetime(2026, 8, 12, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _snap(engine, market, when, bid=44, ask=46, last=45, volume=100):
    with get_session(engine) as s:
        s.add(PriceSnapshot(market_id=market, yes_bid=bid, yes_ask=ask,
                            last_price=last, volume=volume, timestamp=when))
        s.commit()


class TestWriteRateReduction:
    def test_untraded_market_is_not_recorded(self, engine):
        """~69% of snapshots are these, and the scorer skips them anyway."""
        assert record_price_snapshot(engine, "DEAD", 0, 0, 0, 0, now=NOW) is False
        with get_session(engine) as s:
            assert s.query(PriceSnapshot).count() == 0

    def test_traded_market_is_recorded(self, engine):
        assert record_price_snapshot(engine, "LIVE", 44, 46, 45, 100, now=NOW) is True

    def test_unchanged_quote_is_suppressed(self, engine):
        record_price_snapshot(engine, "M", 44, 46, 45, 100, now=NOW)
        wrote = record_price_snapshot(
            engine, "M", 44, 46, 45, 100, now=NOW + dt.timedelta(minutes=5),
        )
        assert wrote is False
        with get_session(engine) as s:
            assert s.query(PriceSnapshot).count() == 1

    def test_a_changed_quote_is_always_recorded(self, engine):
        record_price_snapshot(engine, "M", 44, 46, 45, 100, now=NOW)
        assert record_price_snapshot(
            engine, "M", 45, 47, 46, 120, now=NOW + dt.timedelta(minutes=5),
        ) is True

    def test_heartbeat_keeps_a_quiet_market_from_looking_stale(self, engine):
        """The staleness guard keys on snapshot AGE, so unbroken suppression
        would eventually make a live-but-quiet market indistinguishable from a
        delisted one."""
        record_price_snapshot(engine, "M", 44, 46, 45, 100, now=NOW)
        assert record_price_snapshot(
            engine, "M", 44, 46, 45, 100, now=NOW + dt.timedelta(minutes=25),
        ) is True


class TestSnapshotRetention:
    def test_expired_snapshots_are_removed(self, engine):
        _snap(engine, "M", NOW - dt.timedelta(days=SNAPSHOT_MAX_DAYS + 1))
        _snap(engine, "M", NOW)
        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert s.query(PriceSnapshot).count() == 1

    def test_recent_snapshots_are_kept_at_full_resolution(self, engine):
        for minute in range(0, 60, 5):
            _snap(engine, "M", NOW - dt.timedelta(minutes=minute), last=45 + minute)
        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert s.query(PriceSnapshot).count() == 12

    def test_older_snapshots_are_thinned_to_one_per_hour(self, engine):
        base = NOW - dt.timedelta(days=7)
        for minute in range(0, 60, 5):
            _snap(engine, "M", base + dt.timedelta(minutes=minute), last=45 + minute)
        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert s.query(PriceSnapshot).count() == 1

    def test_thinning_is_per_market(self, engine):
        base = NOW - dt.timedelta(days=7)
        for market in ("A", "B"):
            for minute in (0, 10, 20):
                _snap(engine, market, base + dt.timedelta(minutes=minute))
        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert {m for (m,) in s.query(PriceSnapshot.market_id).all()} == {"A", "B"}


class TestDeltasAreProtected:
    def _delta(self, engine, when):
        with get_session(engine) as s:
            s.add(OrderbookDeltaRaw(
                market_ticker="M", msg_type="delta", sid=1, seq=1,
                payload="{}", received_at=when,
            ))
            s.commit()

    def test_deltas_inside_the_window_are_never_pruned(self, engine):
        """Kalshi serves no historical book: a deleted delta cannot be
        re-collected, so this refuses even under space pressure."""
        self._delta(engine, NOW - dt.timedelta(days=5))
        plan = plan_retention(engine, now=NOW)
        assert any("PROTECTED" in note for note in plan.protected)

        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert s.query(OrderbookDeltaRaw).count() == 1

    def test_deltas_are_pruned_once_the_window_closes(self, engine):
        """The guard must not be a permanent refusal, or it is untested."""
        start = NOW - dt.timedelta(days=DELTA_VALIDATION_DAYS + 5)
        self._delta(engine, start)
        apply_retention(engine, now=NOW)
        with get_session(engine) as s:
            assert s.query(OrderbookDeltaRaw).count() == 0

    def test_window_end_is_measured_from_the_first_delta(self, engine):
        first = NOW - dt.timedelta(days=3)
        self._delta(engine, first)
        self._delta(engine, NOW)
        assert delta_window_end(engine) == first + dt.timedelta(days=DELTA_VALIDATION_DAYS)

    def test_no_deltas_means_no_window_and_no_pruning(self, engine):
        assert delta_window_end(engine) is None


class TestSizeReporting:
    def test_size_is_measured_not_estimated(self, engine):
        _snap(engine, "M", NOW)
        assert database_size_bytes(engine) > 0

    def test_digest_line_warns_above_eighty_percent(self, engine):
        from src.maintenance.retention import RetentionPlan

        plan = RetentionPlan(size_bytes=int(0.85 * 512 * 1024 * 1024))
        assert "⚠️" in format_size_line(plan)

    def test_digest_line_escalates_above_ninety(self, engine):
        from src.maintenance.retention import RetentionPlan

        plan = RetentionPlan(size_bytes=int(0.95 * 512 * 1024 * 1024))
        assert "🚨" in format_size_line(plan)

    def test_healthy_size_is_not_alarming(self, engine):
        from src.maintenance.retention import RetentionPlan

        assert "💾" in format_size_line(RetentionPlan(size_bytes=50 * 1024 * 1024))
