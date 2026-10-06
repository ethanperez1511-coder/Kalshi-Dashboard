"""Every cycle leaves a record, and every successful one pings a dead-man's switch.

GitHub's cron delivered a fraction of the cycles asked for, and on 2026-10-06
the session's own triggers and its watchdog all failed to fire. Nothing
reported it, because a schedule that does not fire produces no error. Two
remedies, both independent of GitHub's scheduler:

  * a digest line counting cycles in the last 24 h against the session's
    expected number, so a schedule gap is a number rather than a silence
  * a ping to an outside service after every successful cycle, which alerts
    when pings STOP (the one failure an inside watchdog cannot see)
"""
from __future__ import annotations

import contextlib
import datetime as dt

import pytest

import src.run_trading as rt
from src.cycle_log import (
    SESSION_EXPECTED,
    cycles_digest,
    format_cycles_digest,
    ping_deadman,
    record_cycle,
)
from src.database import Base

NOW = dt.datetime(2026, 10, 8, 15, 5, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    return db_engine


class TestDigest:
    def test_counts_by_source_against_the_session_expectation(self, engine):
        for i in range(20):
            record_cycle(engine, NOW - dt.timedelta(hours=20, minutes=-15 * i),
                         NOW - dt.timedelta(hours=20, minutes=-15 * i - 3), True, "market-session")
        for h in (2, 9, 13):
            record_cycle(engine, NOW - dt.timedelta(hours=h), NOW - dt.timedelta(hours=h), True,
                         "paper-trade-cycle")
        record_cycle(engine, NOW - dt.timedelta(hours=30), NOW - dt.timedelta(hours=30), True,
                     "market-session")                       # outside 24 h

        line = format_cycles_digest(cycles_digest(engine, now=NOW))

        assert f"session 20/{SESSION_EXPECTED}" in line
        assert "backstop 3" in line
        assert "⚠️" in line                                    # 20 < 24 is a gap

    def test_a_full_session_reads_clean(self, engine):
        for i in range(SESSION_EXPECTED):
            t = NOW - dt.timedelta(hours=20) + dt.timedelta(minutes=15 * i)
            record_cycle(engine, t, t, True, "market-session")
        assert "⚠️" not in format_cycles_digest(cycles_digest(engine, now=NOW))

    def test_failed_cycles_are_counted_apart(self, engine):
        record_cycle(engine, NOW - dt.timedelta(hours=1), NOW, False, "market-session")
        assert "1 failed" in format_cycles_digest(cycles_digest(engine, now=NOW))


class TestDeadman:
    def test_no_url_means_no_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr("httpx.get", lambda *a, **k: calls.append(a))
        assert ping_deadman("") is False and calls == []

    def test_a_failed_ping_never_raises(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr("httpx.get", boom)
        assert ping_deadman("https://hc-ping.com/uuid") is False


class TestWiring:
    """Through run_pipeline, the function every cycle runs (L27)."""

    def _run(self, monkeypatch, engine, body, held=True):
        pings, records = [], []

        @contextlib.contextmanager
        def lock(e):
            yield held

        monkeypatch.setattr(rt, "cycle_lock", lock)
        monkeypatch.setattr(rt, "require_production_database", lambda url: None)
        monkeypatch.setattr(rt, "get_engine", lambda url: engine)
        monkeypatch.setattr(rt, "_run_pipeline_locked", body)
        monkeypatch.setattr(rt, "ping_deadman", lambda url: pings.append(url) or True)
        monkeypatch.setattr(rt, "record_cycle", lambda e, s, f, ok, src: records.append(ok))
        monkeypatch.setenv("DEADMAN_PING_URL", "https://hc-ping.com/uuid")
        return pings, records

    def test_a_successful_cycle_records_and_pings(self, monkeypatch, engine):
        pings, records = self._run(monkeypatch, engine, lambda *a: None)
        rt.run_pipeline()
        assert records == [True] and pings == ["https://hc-ping.com/uuid"]

    def test_a_crashed_cycle_records_a_failure_and_does_not_ping(self, monkeypatch, engine):
        def crash(*a):
            raise RuntimeError("cycle died")

        pings, records = self._run(monkeypatch, engine, crash)
        with pytest.raises(RuntimeError):
            rt.run_pipeline()
        assert records == [False] and pings == []

    def test_a_skipped_cycle_neither_records_nor_pings(self, monkeypatch, engine):
        """The cycle holding the lock will do both; a skip ran nothing."""
        pings, records = self._run(monkeypatch, engine, lambda *a: None, held=False)
        rt.run_pipeline()
        assert records == [] and pings == []
