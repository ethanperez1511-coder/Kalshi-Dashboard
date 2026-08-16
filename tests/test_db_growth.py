"""A level is not a warning. The rate is.

The parlay firehose was discovered at 68% of the tier with two days of headroom,
and the first observation and the emergency were the same event because nothing
tracked the trend. 376 MB is fine on a database that has been 370 MB for a
month and is an emergency on one that was 200 MB on Friday.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.db_growth import TIER_LIMIT_BYTES, format_growth, growth, record_sample
from src.models.db_size import DbSizeSample

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)
MB = 1_048_576


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _sample(engine, days_ago, mb):
    record_sample(
        engine, now=NOW - dt.timedelta(days=days_ago), size_bytes=int(mb * MB),
    )


class TestSampling:
    def test_one_row_per_day(self, engine):
        _sample(engine, 0, 100)
        _sample(engine, 0, 120)

        with get_session(engine) as session:
            sizes = [row.size_bytes for row in session.query(DbSizeSample).all()]

        assert sizes == [120 * MB]

    def test_separate_days_are_separate_rows(self, engine):
        _sample(engine, 1, 100)
        _sample(engine, 0, 120)

        with get_session(engine) as session:
            assert session.query(DbSizeSample).count() == 2


class TestRate:
    def test_mb_per_day_is_the_slope_across_the_window(self, engine):
        _sample(engine, 4, 100)
        _sample(engine, 0, 300)

        result = growth(engine, now=NOW)

        assert result["mb_per_day"] == pytest.approx(50.0)
        assert result["span_days"] == 4

    def test_a_single_sample_reports_no_rate_rather_than_a_fake_one(self, engine):
        """One point is a level. A slope from it would be invented."""
        _sample(engine, 0, 300)

        result = growth(engine, now=NOW)

        assert result["mb_per_day"] is None
        assert result["current_bytes"] == 300 * MB

    def test_an_empty_history_is_not_an_error(self, engine):
        result = growth(engine, now=NOW)

        assert result["samples"] == 0
        assert result["mb_per_day"] is None
        format_growth(result)

    def test_shrinking_reports_a_negative_rate_and_no_deadline(self, engine):
        """After a purge the number must be allowed to go down."""
        _sample(engine, 2, 400)
        _sample(engine, 0, 200)

        result = growth(engine, now=NOW)

        assert result["mb_per_day"] < 0
        assert result["days_to_full"] is None


class TestDaysToFull:
    def test_the_measured_emergency_reproduces(self, engine):
        """The real numbers: ~60 MB/day against ~126 MB of headroom."""
        limit_mb = TIER_LIMIT_BYTES / MB
        _sample(engine, 2, limit_mb - 126 - 120)
        _sample(engine, 0, limit_mb - 126)

        result = growth(engine, now=NOW)

        assert result["mb_per_day"] == pytest.approx(60.0)
        assert result["days_to_full"] == pytest.approx(2.1, abs=0.1)
        assert "FULL IN" in format_growth(result)

    def test_a_calm_database_gets_no_deadline_line(self, engine):
        _sample(engine, 7, 100)
        _sample(engine, 0, 101)

        assert "FULL IN" not in format_growth(growth(engine, now=NOW))
