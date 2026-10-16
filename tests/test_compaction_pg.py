"""Compaction on real Postgres: the guards measure, and the pass pays.

The SQLite tests stub the measurements. This runs the pass on Postgres 16
with a production-shaped table (four indexes, ~250-byte JSON payloads, the
oldest days already pruned so the file holds reusable space, as Neon's did on
2026-10-14) and checks the thing that matters: nulling frees more than it
costs, and the file does not grow past the guard.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import text

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="needs TEST_POSTGRES_URL (real Postgres)")

NOW = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
PAYLOAD = ('{"type":"orderbook_delta","sid":7,"seq":%d,"msg":{"market_ticker":'
           '"KXHIGHNY-26OCT14-T70","price":45,"price_dollars":"0.4500","delta":-10,'
           '"delta_fp":"-10.00","side":"yes","ts":"2026-10-11T15:00:00Z","client_order_id":""}}')


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


def _load(engine, rows_per_day=12_000, days=8):
    with engine.begin() as conn:
        for day in range(days):
            conn.execute(text(
                "INSERT INTO orderbook_delta_raw (market_ticker, msg_type, sid, seq, side,"
                " price_dollars, delta_fp, ts_ms, payload, received_at)"
                " SELECT 'KXHIGHNY-26OCT14-T' || (g % 40), 'delta', 7, g, 'yes', 0.45, -10,"
                " g, format(:p, g), :at FROM generate_series(1, :n) g"
            ), {"p": PAYLOAD.replace("%d", "%s"), "n": rows_per_day,
                "at": NOW - dt.timedelta(days=days - day)})
    # The oldest two days pruned: reusable space inside the file, as in production.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM orderbook_delta_raw WHERE received_at < :c"),
                     {"c": NOW - dt.timedelta(days=days - 2)})
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE orderbook_delta_raw"))


def test_a_full_pass_frees_more_than_it_costs(pg, monkeypatch):
    import src.maintenance.tape as tape

    _load(pg)
    monkeypatch.setattr(tape, "BATCH", 5_000)
    plan = tape.plan_compaction(pg, now=NOW)
    result = tape.compact_tape(pg, plan, now=NOW)
    print(f"\nnulled {result['nulled']:,} freed ~{result['payload_bytes_freed'] / 1e6:.1f} MB "
          f"index {result['index_growth'] / 1e6:+.1f} MB file {result['file_growth'] / 1e6:+.1f} MB "
          f"stopped={result['stopped']!r}")

    assert result["index_growth"] is not None
    assert result["file_growth"] <= tape.FILE_GROWTH_LIMIT_BYTES + tape.BATCH * 400
    if not result["stopped"]:
        assert result["nulled"] == plan.deltas_to_null
        assert result["index_growth"] <= tape.NET_LOSS_RATIO * result["payload_bytes_freed"]


def test_the_daily_sample_keeps_per_table_sizes(pg):
    import json

    from src.db_growth import record_sample, table_sizes
    from src.database import get_session
    from src.models.db_size import DbSizeSample

    _load(pg, rows_per_day=2_000, days=3)
    sizes = table_sizes(pg)
    assert sizes["orderbook_delta_raw"]["heap"] > 0
    assert sizes["orderbook_delta_raw"]["index"] > 0

    record_sample(pg, now=NOW)
    with get_session(pg) as s:
        row = s.query(DbSizeSample).one()
        assert json.loads(row.tables_json)["orderbook_delta_raw"]["heap"] > 0
        assert row.live_bytes is not None


def test_the_emergency_pass_fills_reusable_space_without_growing_the_file(pg):
    """The 2026-10-16 situation: payloads to null, some reusable space from
    pruning, and no room to extend the file."""
    import src.maintenance.tape as tape

    _load(pg)
    plan = tape.plan_compaction(pg, now=NOW)
    result = tape.compact_tape(pg, plan, now=NOW, **tape.EMERGENCY)
    print(f"\nemergency: nulled {result['nulled']:,} of {plan.deltas_to_null:,}, "
          f"freed ~{result['payload_bytes_freed'] / 1e6:.1f} MB, "
          f"index {result['index_growth'] / 1e6:+.2f} MB, file {result['file_growth'] / 1e6:+.2f} MB, "
          f"stopped={result['stopped']!r}")
    assert result["file_growth"] <= tape.EMERGENCY["file_growth_limit"] + 2_000 * 400
    assert result["nulled"] > 0
