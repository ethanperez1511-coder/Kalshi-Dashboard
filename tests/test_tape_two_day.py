"""Ruling 2026-10-06 (a): delta payloads are nulled after 2 days, not 14.

Fourteen days of full payloads would weigh ~640 MB at the measured recording
rate (80k rows/day median), and more under the 15-21 UTC session, which is
over the 500 MB budget and heading for the cap. A delta's payload duplicates
its own columns, so past the 2-day window it is dead weight.

The three guarantees the ruling names, plus the cap guard:
  * a trade or snapshot payload is never touched, at any age
  * a delta inside the 2-day window is never touched
  * a run killed partway keeps every batch it committed, and leaves the
    batch it was killed in fully intact
  * the pass stops at 90% of the cap: its UPDATEs write new row versions
    before VACUUM reclaims the old ones
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

import src.maintenance.tape as tape
from src.database import Base, get_engine, get_session
from src.maintenance.tape import compact_tape, plan_compaction
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.timezone.utc)
PAYLOAD = '{"type":"orderbook_delta","msg":{"market_ticker":"M-1","side":"yes"}}'


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'tape.db'}")
    Base.metadata.create_all(engine)
    return engine


def _rows(engine, msg_type, hours_ago, n=1):
    with get_session(engine) as s:
        for i in range(n):
            s.add(OrderbookDeltaRaw(
                market_ticker="M-1", msg_type=msg_type, sid=1, seq=i,
                side="yes", price_dollars=0.42, delta_fp=3.0, ts_ms=1000 + i,
                payload=PAYLOAD, received_at=NOW - dt.timedelta(hours=hours_ago),
            ))
        s.commit()


def _nulled(engine, msg_type=None):
    with get_session(engine) as s:
        q = select(OrderbookDeltaRaw.payload)
        if msg_type:
            q = q.where(OrderbookDeltaRaw.msg_type == msg_type)
        return [p is None for (p,) in s.execute(q).all()]


def _compact(engine, **kw):
    return compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW, **kw)


def test_a_three_day_old_delta_is_nulled(engine):
    """The ruling itself. Under the 14-day window this delta keeps its JSON."""
    _rows(engine, "delta", hours_ago=72)
    _compact(engine)
    assert _nulled(engine) == [True]


def test_a_delta_inside_two_days_is_never_touched(engine):
    _rows(engine, "delta", hours_ago=47)
    _rows(engine, "delta", hours_ago=1)
    _compact(engine)
    assert _nulled(engine) == [False, False]


@pytest.mark.parametrize("msg_type", ["trade", "snapshot"])
def test_trade_and_snapshot_payloads_are_never_touched_at_any_age(engine, msg_type):
    """fill_sim reads taker_outcome_side, count_fp and is_block_trade from a
    trade payload; a delta stream without its snapshot reconstructs nothing."""
    _rows(engine, msg_type, hours_ago=24 * 13)
    _rows(engine, "delta", hours_ago=24 * 13)
    _compact(engine)
    assert _nulled(engine, msg_type) == [False]
    assert _nulled(engine, "delta") == [True]


def test_a_killed_run_keeps_committed_batches_and_leaves_the_rest_intact(engine, monkeypatch):
    monkeypatch.setattr(tape, "BATCH", 3)
    _rows(engine, "delta", hours_ago=72, n=7)
    calls = []

    def killed_on_second_batch(session):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("runner killed")

    with pytest.raises(RuntimeError):
        _compact(engine, _before_commit=killed_on_second_batch)

    with get_session(engine) as s:
        rows = s.execute(select(OrderbookDeltaRaw.payload)).all()
    assert len(rows) == 7                                  # no row lost
    assert sum(p is None for (p,) in rows) == 3            # batch 1 kept
    assert sum(p == PAYLOAD for (p,) in rows) == 4         # batch 2 intact, untouched rest

    _compact(engine)                                       # a re-run finishes the job
    assert all(_nulled(engine))


def test_batches_never_exceed_twenty_thousand_rows():
    assert tape.BATCH <= 20_000


def test_it_stops_at_ninety_percent_of_the_cap(engine, monkeypatch):
    from src.maintenance.retention import NEON_CAP_BYTES

    monkeypatch.setattr(tape, "BATCH", 2)
    monkeypatch.setattr(tape, "database_size_bytes", lambda e: int(0.91 * NEON_CAP_BYTES))
    _rows(engine, "delta", hours_ago=72, n=6)

    result = _compact(engine)

    assert result["nulled"] == 0
    assert "cap" in result["stopped"]
    assert not any(_nulled(engine))
