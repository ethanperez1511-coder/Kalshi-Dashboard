"""Every read-only dispatch must fail when it measures nothing.

The day-7 job was dispatched, exited 0, showed a green check, and produced no
run summary and a two-line header in the log. It had "succeeded" while
answering nothing — the exact failure mode this project has a standing rule
against, one level up from a test that skips itself.

The class test at the bottom is the point. Fixing `day7` alone would leave the
next read-only action free to make the same mistake; this asserts that every
one of them fails on an empty database, which is the condition under which
"produced no report" is unambiguous.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_engine, get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.report_guard import publish_report

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def summary(monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


class TestPublishReport:
    def test_a_substantive_report_succeeds_and_is_written(self, summary):
        code = publish_report("Day-7", "sports: 412 prints over 30h", True)

        assert code == 0
        text = summary.read_text()
        assert "412 prints" in text
        assert "✅" in text

    def test_an_empty_report_fails(self, summary):
        assert publish_report("Day-7", "header only\n", False) == 1

    def test_an_empty_report_says_so_in_the_summary(self, summary):
        publish_report("Day-7", "header only\n", False)
        text = summary.read_text()

        assert "❌" in text
        assert "NO REPORT" in text

    def test_emptiness_is_not_inferred_from_the_text(self, summary):
        """A header rendered over an empty result set is exactly the case that
        fooled the operator. Long output with nothing in it must still fail."""
        header = "Day-7 trade-through measurement (per category, never pooled)\n" * 5

        assert publish_report("Day-7", header, False) == 1


class TestDay7:
    def _engine(self, tmp_path, name):
        engine = get_engine(f"sqlite:///{tmp_path / name}.db")
        Base.metadata.create_all(engine)
        return engine

    def _seed(self, engine, msg_type, live=True):
        with get_session(engine) as session:
            session.add(Market(
                market_id="M-1", title="t", category="Weather",
                close_date=NOW + (dt.timedelta(days=1) if live else -dt.timedelta(days=1)),
                status="active",
            ))
            for hour in range(30):
                session.add(OrderbookDeltaRaw(
                    market_ticker="M-1", msg_type=msg_type, ts_ms=1000 + hour,
                    payload='{"msg": {"yes_price_dollars": "0.42"}}',
                    received_at=NOW - dt.timedelta(hours=hour),
                ))
            session.commit()

    def test_deltas_but_no_trade_prints_is_a_failure(self, tmp_path, summary, monkeypatch):
        """The production case. 253,261 recorded messages, overwhelmingly
        deltas on quiet books, and not one trade print to measure."""
        engine = self._engine(tmp_path, "d1")
        self._seed(engine, "delta")

        from src.execution import day7

        monkeypatch.setattr(day7, "get_engine", lambda url: engine)
        assert day7.main([]) == 1
        assert "NO REPORT" in summary.read_text()

    def test_trade_prints_produce_a_report(self, tmp_path, summary, monkeypatch):
        engine = self._engine(tmp_path, "d2")
        self._seed(engine, "trade")

        from src.execution import day7

        monkeypatch.setattr(day7, "get_engine", lambda url: engine)
        assert day7.main([]) == 0
        assert "WeatherModel" in summary.read_text()

    def test_no_recorder_data_at_all_still_fails_loudly(
        self, tmp_path, summary, monkeypatch,
    ):
        engine = self._engine(tmp_path, "d3")

        from src.execution import day7

        monkeypatch.setattr(day7, "get_engine", lambda url: engine)
        assert day7.main([]) == 1
        assert summary.read_text().strip(), "an empty run wrote no summary at all"


class TestEveryReadOnlyDispatchFailsOnEmpty:
    """The class, not the instance.

    Fixing day-7 alone leaves the next read-only action free to repeat it.
    """

    def _empty_engine(self, tmp_path, name):
        engine = get_engine(f"sqlite:///{tmp_path / name}.db")
        Base.metadata.create_all(engine)
        return engine

    def test_day7_fails_on_an_empty_database(self, tmp_path, summary, monkeypatch):
        from src.execution import day7

        engine = self._empty_engine(tmp_path, "e1")
        monkeypatch.setattr(day7, "get_engine", lambda url: engine)

        assert day7.main([]) == 1

    def test_db_stats_fails_on_an_empty_database(self, tmp_path, summary):
        from src.maintenance.__main__ import _db_stats

        engine = self._empty_engine(tmp_path, "e2")

        assert _db_stats(engine) == 1
        assert "NO REPORT" in summary.read_text()

    def test_db_stats_succeeds_when_there_is_something_to_report(
        self, tmp_path, summary,
    ):
        from src.maintenance.__main__ import _db_stats

        engine = self._empty_engine(tmp_path, "e3")
        with get_session(engine) as session:
            session.add(Market(
                market_id="M-1", title="t", category="Weather",
                close_date=NOW + dt.timedelta(days=1), status="active",
            ))
            session.commit()

        assert _db_stats(engine) == 0
        assert "❌" not in summary.read_text()
