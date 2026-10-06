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


class TestComputeIsReportedInCuHours:
    """Neon bills CU-hours: awake time x compute size, at a 0.25 CU floor here.
    The digest reported awake WALL hours as if they were the bill, and was ~6x
    high against the console (2026-10-06: ~49 "h" in the digest, 7.69 CU-h in
    the console, 0.25<->2 CU autoscaling)."""

    def _october_6(self, engine):
        """The inputs behind the 2026-10-06 digest: 39 recorded hours over six
        sampled days, measured October 1-6."""
        from src.models.orderbook_raw import OrderbookDeltaRaw

        start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        with get_session(engine) as s:
            for h in range(39):
                s.add(OrderbookDeltaRaw(
                    market_ticker="M", msg_type="delta", sid=1, seq=h, payload="{}",
                    received_at=start + dt.timedelta(hours=h * 3),
                ))
            for d in range(6):
                s.add(TransferSample(sampled_on=(start + dt.timedelta(days=d)).date()))
            s.commit()
        return dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)

    def test_the_estimate_brackets_the_console_from_above(self, engine):
        """At the 0.25 CU floor the estimate must not undershoot the bill, or
        it hides the deadline it exists to show, and must not be the 6x
        overstatement that turned a non-problem into an emergency."""
        now = self._october_6(engine)
        data = transfer_meter.compute_estimate(engine, now=now)

        console = 7.69
        assert console <= data["cu_hours"] <= 2 * console

    def test_the_line_names_the_unit_and_the_authority(self, engine):
        line = transfer_meter.format_compute(
            transfer_meter.compute_estimate(engine, now=self._october_6(engine))
        )
        assert "CU-h" in line
        assert "console" in line
