"""2026-10-14: the heartbeat disagreed with itself.

Retention said headroom ~194 MB (cap minus live data); the heartbeat's DB line
said ~92 MB (cap minus the file) and "NEON CAP IN ~3.8 DAYS" from file growth.
Two causes: the heartbeat line never passed live data to size_status, and the
projection used live data only when EVERY sample in its 7-day window had it.
Samples from before 2026-10-13 have none, so it fell back to the file.

And a day's growth could not be broken down by table from the digest: the
per-table sizes were only in a retention log. Now each daily sample keeps
them, and the digest prints what moved.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base
from src.db_growth import format_growth, growth, record_sample

MB = 1_000_000
TODAY = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    import src.models  # noqa: F401
    Base.metadata.create_all(db_engine)
    return db_engine


def _day(engine, days_ago, file_mb, live_mb=None, tables=None):
    record_sample(engine, now=TODAY - dt.timedelta(days=days_ago),
                  size_bytes=file_mb * MB,
                  live_bytes=None if live_mb is None else live_mb * MB,
                  tables=tables)


class TestOneHeadroom:
    def test_the_heartbeat_line_counts_reusable_space(self, engine, monkeypatch):
        import src.maintenance.retention as retention
        monkeypatch.setattr(retention, "live_bytes_estimate", lambda e: None)
        _day(engine, 1, 903, 745)
        _day(engine, 0, 908, 804)
        text = format_growth(growth(engine, now=TODAY))
        assert "headroom ~196 MB" in text           # 1000 - 804
        assert "headroom ~92 MB" not in text        # 1000 - 908: the file


class TestProjectionBasis:
    def test_old_samples_without_live_do_not_force_a_file_projection(self, engine):
        # Oct 10-12: file only (before the column existed). Oct 13-14: live.
        _day(engine, 4, 814)
        _day(engine, 3, 830)
        _day(engine, 2, 860)
        _day(engine, 1, 903, 745)
        _day(engine, 0, 908, 804)
        result = growth(engine, now=TODAY)
        assert result["basis"] == "live"
        assert result["mb_per_day"] == pytest.approx(59.0)
        assert result["days_to_full"] == pytest.approx(196 / 59, rel=0.01)

    def test_fewer_than_two_live_days_print_no_deadline(self, engine):
        _day(engine, 3, 814)
        _day(engine, 2, 860)
        _day(engine, 0, 908, 804)
        result = growth(engine, now=TODAY)
        assert result["days_to_full"] is None
        assert "NEON CAP IN" not in format_growth(result)


class TestTableBreakdown:
    def test_the_digest_names_what_grew(self, engine):
        _day(engine, 1, 903, 745, tables={
            "orderbook_delta_raw": {"heap": 500 * MB, "index": 150 * MB, "toast": 0},
            "price_snapshots": {"heap": 40 * MB, "index": 10 * MB, "toast": 0},
        })
        _day(engine, 0, 908, 804, tables={
            "orderbook_delta_raw": {"heap": 538 * MB, "index": 162 * MB, "toast": 0},
            "price_snapshots": {"heap": 45 * MB, "index": 11 * MB, "toast": 0},
        })
        result = growth(engine, now=TODAY)
        assert result["table_deltas"][0] == ("orderbook_delta_raw", 38 * MB, 12 * MB, 0)
        text = format_growth(result)
        assert "orderbook_delta_raw +50 MB (heap +38, index +12)" in text
        assert "price_snapshots +6 MB" in text

    def test_no_previous_breakdown_prints_none(self, engine):
        _day(engine, 1, 903, 745)
        _day(engine, 0, 908, 804, tables={"t": {"heap": MB, "index": 0, "toast": 0}})
        assert "by table" not in format_growth(growth(engine, now=TODAY))
