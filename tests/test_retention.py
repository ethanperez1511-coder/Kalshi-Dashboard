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
    CAP_ALARM_FRACTION,
    DELTA_RETENTION_DAYS,
    NEON_CAP_BYTES,
    SNAPSHOT_MAX_DAYS,
    STORAGE_BUDGET_BYTES,
    RetentionPlan,
    apply_retention,
    database_size_bytes,
    format_plan,
    format_size_line,
    plan_retention,
    prune_tape,
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


class TestTapeRetention:
    """Rolling-window behaviour is in test_retention_rolling.py. These pin the
    mechanics: the plan counts what apply removes, and deletes are batched."""

    def _delta(self, engine, when):
        with get_session(engine) as s:
            s.add(OrderbookDeltaRaw(
                market_ticker="M", msg_type="delta", sid=1, seq=1,
                payload="{}", received_at=when,
            ))
            s.commit()

    def test_plan_counts_exactly_what_apply_removes(self, engine):
        for age in (DELTA_RETENTION_DAYS + 3, DELTA_RETENTION_DAYS + 1, 2):
            self._delta(engine, NOW - dt.timedelta(days=age))
        planned = plan_retention(engine, now=NOW).deletions["orderbook_tape_expired"]
        applied = apply_retention(engine, now=NOW).deletions["orderbook_tape_expired"]
        assert planned == applied == 2

    def test_deletes_run_in_bounded_batches(self, engine, monkeypatch):
        import src.maintenance.retention as retention

        monkeypatch.setattr(retention, "DELETE_BATCH", 3)
        for _ in range(10):
            self._delta(engine, NOW - dt.timedelta(days=DELTA_RETENTION_DAYS + 1))
        self._delta(engine, NOW)

        assert prune_tape(engine, NOW) == 10
        with get_session(engine) as s:
            assert s.query(OrderbookDeltaRaw).count() == 1

    def test_the_window_is_never_wider_than_the_cap_can_hold(self):
        """At the measured +49.5 MB/day, the window must fit inside the
        budget with room for everything else. 60 days was ~3 GB."""
        assert DELTA_RETENTION_DAYS * 49.5 * 1_000_000 < NEON_CAP_BYTES


class TestBudgetAndCap:
    """The cap is Neon's; the budget is ours. Neither may drift into the other."""

    def test_the_budget_sits_well_under_the_cap(self):
        """The September lesson: headroom. At least 40% of the cap stays
        free when the budget is first breached."""
        assert STORAGE_BUDGET_BYTES <= 0.6 * NEON_CAP_BYTES

    def test_the_cap_is_the_current_neon_free_limit_read_conservatively(self):
        """1 GB as the console counts it (decimal), not 1 GiB."""
        assert NEON_CAP_BYTES == 1_000_000_000

    def test_size_is_measured_not_estimated(self, engine):
        _snap(engine, "M", NOW)
        assert database_size_bytes(engine) > 0

    def test_under_budget_is_calm(self):
        line = format_size_line(RetentionPlan(size_bytes=400 * 1_000_000))
        assert line.startswith("💾")

    def test_over_budget_warns_long_before_the_cap(self):
        plan = RetentionPlan(size_bytes=600 * 1_000_000)
        assert plan.over_budget and not plan.cap_alarm
        assert format_size_line(plan).startswith("⚠️")

    def test_near_the_cap_escalates(self):
        plan = RetentionPlan(size_bytes=int(CAP_ALARM_FRACTION * NEON_CAP_BYTES) + 1)
        assert format_size_line(plan).startswith("🚨")
        assert "refuses writes AND deletes" in format_plan(plan, applied=True)

    def test_todays_size_reads_as_the_console_does(self):
        """The console said 762 MB on 2026-10-06, while the heartbeat said
        '721 MB (134% of free tier)': MiB against a stale 512 MiB constant.
        The same bytes must now print the console's number."""
        line = format_size_line(RetentionPlan(size_bytes=761_960_000))
        assert line.startswith("⚠️ DB: 762 MB")
        assert "152% of 500 MB budget" in line
        assert "76% of 1000 MB Neon cap" in line
        assert line.startswith("⚠️")
