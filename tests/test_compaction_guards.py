"""2026-10-14: compaction did 200k of an 892k backlog and stopped at its cap.

The fixed run cap stood in for a risk (each nulled row adds index entries).
It is replaced by guards that measure that risk and stop the pass the moment
it stops paying: the file grew, indexes outgrew the payload freed, or the
time budget ran out. Retention runs three times a day so the backlog clears.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.maintenance.tape as tape
from src.database import Base, get_engine, get_session
from src.models.orderbook_raw import OrderbookDeltaRaw

MB = 1_000_000
NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def tape_engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(e)
    with get_session(e) as s:
        for i in range(10):
            s.add(OrderbookDeltaRaw(market_ticker="M", msg_type="delta", sid=1, seq=i,
                                    payload="x" * 100,
                                    received_at=NOW - dt.timedelta(days=3)))
        s.commit()
    return e


def _run(engine):
    return tape.compact_tape(engine, tape.plan_compaction(engine, now=NOW), now=NOW)


def _nulled(engine):
    with get_session(engine) as s:
        return s.query(OrderbookDeltaRaw).filter(OrderbookDeltaRaw.payload.is_(None)).count()


class TestCompactionGuards:
    def test_the_backlog_clears_in_one_run(self):
        assert tape.MAX_ROWS_PER_RUN >= 1_000_000

    def test_within_the_guards_everything_is_nulled(self, tape_engine, monkeypatch):
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: 900 * MB)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
        result = _run(tape_engine)
        assert result["nulled"] == 10 and not result["stopped"]

    def test_it_stops_when_the_file_grows(self, tape_engine, monkeypatch):
        sizes = iter([900 * MB, 900 * MB, 909 * MB] + [909 * MB] * 20)
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: next(sizes))
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
        result = _run(tape_engine)
        assert "file grew" in result["stopped"]
        assert 0 < result["nulled"] < 10

    def test_it_stops_when_index_growth_outweighs_the_payload_freed(self, tape_engine, monkeypatch):
        # 3 rows x 100 bytes of payload freed per batch; indexes grow 2 MB.
        index = iter([100 * MB] + [102 * MB] * 20)
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: 900 * MB)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: next(index))
        result = _run(tape_engine)
        assert "net loss" in result["stopped"]
        assert result["nulled"] == 3

    def test_page_sized_index_growth_on_a_small_pass_does_not_stop_it(self, tape_engine, monkeypatch):
        index = iter([100 * MB] + [100 * MB + 8192 * 3] * 20)
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: 900 * MB)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: next(index))
        assert _run(tape_engine)["nulled"] == 10

    def test_it_stops_at_the_time_budget(self, tape_engine, monkeypatch):
        clock = iter([0.0] + [tape.TIME_BUDGET_SECONDS + 1] * 20)
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: 900 * MB)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
        monkeypatch.setattr(tape.time, "monotonic", lambda: next(clock))
        result = _run(tape_engine)
        assert "time budget" in result["stopped"]
        assert result["nulled"] == 3

    def test_the_result_reports_the_bytes_actually_freed(self, tape_engine, monkeypatch):
        monkeypatch.setattr(tape, "BATCH", 3)
        monkeypatch.setattr(tape, "database_size_bytes", lambda e: 900 * MB)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
        index = iter([100 * MB] + [101 * MB] * 20)
        monkeypatch.setattr(tape, "tape_index_bytes", lambda e: next(index))
        result = _run(tape_engine)
        assert result["payload_bytes_freed"] == result["nulled"] * 100 == 1000
        assert result["index_growth"] == 1 * MB

    def test_sqlite_has_no_measurements_and_is_not_stopped_by_them(self, tape_engine):
        assert tape.tape_index_bytes(tape_engine) is None
        assert _run(tape_engine)["nulled"] == 10


def test_retention_runs_three_times_a_day_with_room_to_finish():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/retention.yml"))
    crons = [c["cron"] for c in wf[True]["schedule"]]
    assert len(crons) == 3
    assert wf["jobs"]["prune"]["timeout-minutes"] >= 30
