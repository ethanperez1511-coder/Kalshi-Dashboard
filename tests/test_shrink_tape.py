"""In-place tape shrink: what it may change and what it must never change.

The shrink depends on Postgres physical layout (ctid order, VACUUM truncating
trailing empty pages), which SQLite cannot reproduce. Its behavioural tests
therefore run against a real Postgres named by TEST_POSTGRES_URL (L15), e.g.

    docker run -d --rm -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
    TEST_POSTGRES_URL=postgresql+psycopg://postgres:pw@localhost:55432/postgres pytest tests/test_shrink_tape.py

The projection math and the token gate need no database and always run.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import text

import src.maintenance.shrink_tape as shrink_tape
from src.maintenance.shrink_tape import (
    CONFIRM_TOKEN,
    IndexStat,
    TapeMeasure,
    TypeStat,
    guard_refusal,
    measure,
    project_live_bytes,
    shrink,
    steady_state_bytes,
)
from tests.test_maintenance_entrypoint import run_module, seeded  # noqa: F401

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="needs TEST_POSTGRES_URL (real Postgres)")
MB = 1_000_000
TODAY = dt.date(2026, 10, 7)


# --------------------------------------------------------------------------
# Projection math — always runs
# --------------------------------------------------------------------------

def _measure(rows_by_day, delta_rows=900, other_rows=100, tuple_b=500, payload_b=300):
    m = TapeMeasure(supported=True, db_size=700 * MB, heap_size=500 * MB)
    m.by_type = [
        TypeStat("delta", delta_rows, delta_rows * tuple_b, delta_rows * payload_b),
        TypeStat("trade", other_rows, other_rows * tuple_b, other_rows * payload_b),
    ]
    m.indexes = [IndexStat("ix", ["id"], 10 * MB, estimate=0)]
    m.rows_by_day = rows_by_day
    return m


class TestProjection:
    def test_rate_ignores_the_partial_first_and_last_days(self):
        m = _measure([(TODAY - dt.timedelta(days=4), 10), (TODAY - dt.timedelta(days=3), 1000),
                      (TODAY - dt.timedelta(days=2), 1200), (TODAY - dt.timedelta(days=1), 1100),
                      (TODAY, 40)])
        assert m.rows_per_day == 1100

    def test_steady_state_is_rate_times_window(self):
        m = _measure([(TODAY - dt.timedelta(days=d), 1000) for d in range(5, -1, -1)])
        assert steady_state_bytes(m, 14) == int(1000 * 14 * m.bytes_per_row)

    def test_slim_payloads_save_only_delta_payload_bytes_outside_the_full_days(self):
        m = _measure([(TODAY - dt.timedelta(days=d), 1000) for d in range(5, -1, -1)])
        full = steady_state_bytes(m, 14)
        slim = steady_state_bytes(m, 14, full_payload_days=2)
        # 12 slim days x 1000 rows x 90% deltas x 300 B payload
        assert full - slim == 12 * 1000 * 0.9 * 300

    def test_projection_stops_growing_once_the_window_is_full(self):
        m = _measure([(TODAY - dt.timedelta(days=d), 1000) for d in range(5, -1, -1)])
        future = project_live_bytes(m, days_ahead=30, window_days=14, now=TODAY)
        assert future[20] == future[29]          # plateau
        assert future[0] < future[20]            # filling until then


class TestGuard:
    def test_a_chunk_that_would_cross_the_guard_line_is_refused(self):
        assert guard_refusal(940 * MB, 20 * MB) is not None

    def test_a_chunk_with_room_is_allowed(self):
        assert guard_refusal(740 * MB, 20 * MB) is None


class TestTokenGate:
    def test_dry_run_on_sqlite_says_so_and_is_green(self, seeded, monkeypatch, capsys):
        code, out = run_module(["--shrink-tape"], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "not a Postgres database" in out

    @pytest.mark.parametrize("bad", ["SHRINK", "VACUUM-FULL-TAPE", "PURGE-ORPHAN-MARKETS"])
    def test_any_other_token_changes_nothing(self, seeded, monkeypatch, capsys, bad):
        code, _ = run_module(["--shrink-tape", "--confirm", bad], seeded, monkeypatch, capsys)
        assert code == 2

    def test_its_token_does_not_authorise_a_vacuum_full(self, seeded, monkeypatch, capsys):
        code, _ = run_module(["--vacuum-full", "--confirm", CONFIRM_TOKEN], seeded, monkeypatch, capsys)
        assert code == 2


# --------------------------------------------------------------------------
# Real Postgres
# --------------------------------------------------------------------------

LONG_AGO = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def pg():
    from src.database import Base, get_engine
    import src.models  # noqa: F401

    engine = get_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_production_shape(engine, dead=24_000, live=8_000):
    """The shape retention left behind: a long run of deleted rows at the
    front of the file and the live tape packed at the tail."""
    payload = '{"type":"orderbook_delta","msg":{"pad":"' + "x" * 380 + '"}}'
    with engine.begin() as c:
        for start, n, when in ((0, dead, LONG_AGO), (dead, live, NOW - dt.timedelta(hours=2))):
            c.execute(text(
                "INSERT INTO orderbook_delta_raw "
                "(market_ticker, msg_type, sid, seq, payload, received_at) "
                "SELECT 'KXHIGHNY-26OCT07-T70', 'delta', 1, g, :p, :w "
                "FROM generate_series(CAST(:a AS integer), CAST(:b AS integer)) g"
            ), {"p": payload, "w": when, "a": start, "b": start + n - 1})
        c.execute(text("DELETE FROM orderbook_delta_raw WHERE received_at < :c"),
                  {"c": LONG_AGO + dt.timedelta(days=1)})
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM ANALYZE orderbook_delta_raw"))


def _fingerprint(engine):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT count(*), sum(id), md5(string_agg(id::text || ':' || coalesce(payload, ''), ',' ORDER BY id)) "
            "FROM orderbook_delta_raw"
        )).one()


def _heap(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT pg_relation_size('orderbook_delta_raw', 'main')")).scalar()


@needs_pg
class TestShrinkOnPostgres:
    def test_measure_changes_nothing(self, pg):
        _seed_production_shape(pg)
        before, heap = _fingerprint(pg), _heap(pg)
        m = measure(pg)
        assert m.rows == 8_000
        assert m.packed_heap < m.heap_size
        assert _fingerprint(pg) == before and _heap(pg) == heap

    def test_shrink_returns_the_file_and_keeps_every_row(self, pg):
        _seed_production_shape(pg)
        before, heap_before = _fingerprint(pg), _heap(pg)

        result = shrink(pg, measure(pg), now=NOW, chunk_rows=2_000)

        assert result.refused is None
        assert _fingerprint(pg) == before                 # same ids, same payloads
        assert _heap(pg) < heap_before * 0.45             # live rows were ~25% of the file

    def test_a_chunk_that_fails_midway_leaves_the_table_intact(self, pg):
        """The required failure test. The chunk's DELETE has run and its
        INSERT has not been committed when the failure fires. Every row must
        still be there, unchanged, and the file must not have grown."""
        _seed_production_shape(pg)
        before, heap_before = _fingerprint(pg), _heap(pg)

        def boom(conn):
            raise RuntimeError("killed mid-chunk")

        with pytest.raises(RuntimeError):
            shrink(pg, measure(pg), now=NOW, chunk_rows=2_000, _after_delete=boom)

        assert _fingerprint(pg) == before
        assert _heap(pg) <= heap_before

    def test_the_space_guard_stops_before_any_move(self, pg, monkeypatch):
        _seed_production_shape(pg)
        before = _fingerprint(pg)
        monkeypatch.setattr(shrink_tape, "NEON_CAP_BYTES", 1)   # no headroom at all

        result = shrink(pg, measure(pg), now=NOW, chunk_rows=2_000)

        assert result.rounds == 0 and "space guard" in result.stopped
        assert _fingerprint(pg) == before

    def test_it_waits_for_a_quiet_recorder(self, pg):
        _seed_production_shape(pg)
        before = _fingerprint(pg)
        m = measure(pg)

        result = shrink(pg, m, now=m.last_received + dt.timedelta(minutes=1))

        assert result.refused and "recorder is writing" in result.refused
        assert _fingerprint(pg) == before
