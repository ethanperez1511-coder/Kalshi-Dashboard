"""Transfer needs a number before it needs an outage.

Storage had a growth line; transfer had nothing. So the first anyone heard of
the transfer quota was Neon closing every connection at 100% — no warning, no
trend, production simply stopped.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src import transfer_meter
from src.database import Base, get_engine, get_session
from src.models.transfer import TransferSample

NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.timezone.utc)
GB = 1_000_000_000


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'meter.db'}")
    Base.metadata.create_all(engine)
    transfer_meter.reset()
    return engine


def _sample(engine, day_offset, gb):
    with get_session(engine) as session:
        session.add(TransferSample(
            sampled_on=(NOW - dt.timedelta(days=day_offset)).date(),
            bytes_in=0, bytes_out=int(gb * GB), statements=1,
        ))
        session.commit()


class TestMetering:
    def test_statements_are_counted(self, engine):
        transfer_meter.attach(engine)
        with get_session(engine) as session:
            session.execute(TransferSample.__table__.select())

        assert transfer_meter.totals()["statements"] >= 1

    def test_result_rows_are_charged_as_egress(self, engine):
        transfer_meter.record_result_rows([("KXHIGHNY-26AUG24-T90", 1), ("x", 2)])

        assert transfer_meter.totals()["out"] > 0

    def test_flush_folds_into_todays_row(self, engine):
        transfer_meter.record_result_rows([("a" * 100,)])
        transfer_meter.flush(engine, now=NOW)
        transfer_meter.record_result_rows([("b" * 100,)])
        result = transfer_meter.flush(engine, now=NOW)

        with get_session(engine) as session:
            assert session.query(TransferSample).count() == 1
        assert result["bytes_out"] >= 200

    def test_flush_resets_so_a_cycle_is_not_counted_twice(self, engine):
        transfer_meter.record_result_rows([("a" * 100,)])
        transfer_meter.flush(engine, now=NOW)

        assert transfer_meter.totals()["out"] == 0

    def test_metering_never_raises_on_odd_values(self, engine):
        transfer_meter.record_result_rows([(None, object(), 3.5)])  # must not raise


class TestReporting:
    def test_a_quiet_month_does_not_warn(self, engine):
        _sample(engine, 1, 0.05)
        _sample(engine, 0, 0.05)

        text = transfer_meter.format_transfer(
            transfer_meter.month_to_date(engine, now=NOW)
        )

        assert "⚠️" not in text

    def test_crossing_the_warn_fraction_says_so(self, engine):
        _sample(engine, 1, 2.0)
        _sample(engine, 0, 1.6)

        data = transfer_meter.month_to_date(engine, now=NOW)
        text = transfer_meter.format_transfer(data)

        assert data["fraction"] >= 0.70
        assert "⚠️" in text
        assert "headroom" in text

    def test_it_projects_days_of_headroom(self, engine):
        _sample(engine, 1, 2.0)
        _sample(engine, 0, 1.8)

        text = transfer_meter.format_transfer(
            transfer_meter.month_to_date(engine, now=NOW)
        )

        assert "days of headroom" in text

    def test_no_samples_is_stated_not_silent(self, engine):
        text = transfer_meter.format_transfer(
            transfer_meter.month_to_date(engine, now=NOW)
        )

        assert "no samples" in text

    def test_only_this_month_counts(self, engine):
        """A quota that resets monthly must not carry last month's bytes."""
        _sample(engine, 40, 4.9)
        _sample(engine, 0, 0.01)

        data = transfer_meter.month_to_date(engine, now=NOW)

        assert data["days"] == 1
        assert data["fraction"] < 0.1
