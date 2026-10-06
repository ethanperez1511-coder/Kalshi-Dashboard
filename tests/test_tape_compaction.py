"""Null delta payloads OUTSIDE the replay window. Never inside it.

`orderbook_delta_raw.payload` stores the complete raw JSON of every message,
on the stated principle that reconstruction logic will change and re-deriving
from the original message must always be possible.

For DELTA messages that principle buys nothing after a point: the row already
denormalises market_ticker, sid, seq, side, price_dollars, delta_fp and ts_ms,
which is the entire content of a delta. Keeping the JSON too roughly doubles
the write.

But it buys everything INSIDE the window replay and shadow actually read, so
the carve-out is the whole design: payloads stay full for COMPACTION_WINDOW_DAYS
(2 since ruling 2026-10-06 (a); see test_tape_two_day.py) and are nulled only
after. Trade prints keep their payload for the full retention
period regardless — `fill_sim` reads taker_outcome_side, count_fp and
is_block_trade from it, and none of those are denormalised.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from src.database import Base, get_engine, get_session
from src.maintenance.tape import COMPACTION_WINDOW_DAYS, compact_tape, plan_compaction
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.timezone.utc)
PAYLOAD = '{"type":"orderbook_delta","msg":{"market_ticker":"M-1","side":"yes"}}'


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'tape.db'}")
    Base.metadata.create_all(engine)
    return engine


def _row(engine, msg_type, days_ago, payload=PAYLOAD):
    with get_session(engine) as session:
        session.add(OrderbookDeltaRaw(
            market_ticker="M-1", msg_type=msg_type, sid=1, seq=1,
            side="yes", price_dollars=0.42, delta_fp=3.0, ts_ms=1000,
            payload=payload, received_at=NOW - dt.timedelta(days=days_ago),
        ))
        session.commit()


def _payloads(engine, msg_type=None):
    with get_session(engine) as session:
        stmt = select(OrderbookDeltaRaw.msg_type, OrderbookDeltaRaw.payload)
        if msg_type:
            stmt = stmt.where(OrderbookDeltaRaw.msg_type == msg_type)
        return session.execute(stmt).all()


class TestTheWindowIsRespected:
    def test_a_delta_inside_the_window_keeps_its_payload(self, engine):
        """Replay and shadow read recent tape. Re-derivability is load-bearing
        exactly here."""
        _row(engine, "delta", days_ago=COMPACTION_WINDOW_DAYS - 0.5)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        assert _payloads(engine)[0][1] == PAYLOAD

    def test_a_delta_outside_the_window_is_nulled(self, engine):
        _row(engine, "delta", days_ago=30)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        assert _payloads(engine)[0][1] is None

    def test_the_boundary_belongs_to_the_window(self, engine):
        """A row exactly at the edge is kept. Off-by-one here silently costs a
        day of replay."""
        _row(engine, "delta", days_ago=COMPACTION_WINDOW_DAYS - 0.1)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        assert _payloads(engine)[0][1] == PAYLOAD


class TestOnlyDeltasAreTouched:
    @pytest.mark.parametrize("msg_type", ["trade", "snapshot"])
    def test_other_message_types_keep_their_payload_forever(self, engine, msg_type):
        """Trades carry taker_outcome_side, count_fp and is_block_trade, none
        of which are denormalised. Snapshots are the anchor a delta stream is
        applied to — a delta stream without one reconstructs nothing."""
        _row(engine, msg_type, days_ago=90)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        assert _payloads(engine)[0][1] == PAYLOAD

    def test_a_mixed_tape_compacts_only_the_deltas(self, engine):
        _row(engine, "delta", days_ago=30)
        _row(engine, "trade", days_ago=30)
        _row(engine, "snapshot", days_ago=30)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        by_type = dict(_payloads(engine))
        assert by_type["delta"] is None
        assert by_type["trade"] == PAYLOAD
        assert by_type["snapshot"] == PAYLOAD


class TestTheDenormalisedColumnsSurvive:
    def test_nulling_the_payload_keeps_every_reconstruction_field(self, engine):
        """The justification for nulling at all: the row already holds the
        delta's entire content."""
        _row(engine, "delta", days_ago=30)

        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        with get_session(engine) as session:
            row = session.execute(
                select(
                    OrderbookDeltaRaw.market_ticker, OrderbookDeltaRaw.sid,
                    OrderbookDeltaRaw.seq, OrderbookDeltaRaw.side,
                    OrderbookDeltaRaw.price_dollars, OrderbookDeltaRaw.delta_fp,
                    OrderbookDeltaRaw.ts_ms,
                )
            ).one()

        assert row == ("M-1", 1, 1, "yes", 0.42, 3.0, 1000)


class TestDryRunAndVisibility:
    def test_planning_changes_nothing(self, engine):
        _row(engine, "delta", days_ago=30)

        plan_compaction(engine, now=NOW)

        assert _payloads(engine)[0][1] == PAYLOAD

    def test_the_plan_counts_what_it_would_null(self, engine):
        for _ in range(4):
            _row(engine, "delta", days_ago=30)
        _row(engine, "delta", days_ago=2)
        _row(engine, "trade", days_ago=30)

        plan = plan_compaction(engine, now=NOW)

        assert plan.deltas_to_null == 4
        assert plan.bytes_reclaimed > 0

    def test_running_twice_is_idempotent(self, engine):
        _row(engine, "delta", days_ago=30)
        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        second = plan_compaction(engine, now=NOW)

        assert second.deltas_to_null == 0

    def test_the_report_names_the_window_and_the_carve_out(self, engine):
        from src.maintenance.tape import format_compaction

        _row(engine, "delta", days_ago=30)
        text = format_compaction(plan_compaction(engine, now=NOW))

        assert f"{COMPACTION_WINDOW_DAYS} days" in text
        assert "trade" in text.lower()


class TestConsumersFailSafeOnACompactedRow:
    """Compaction makes NULL payloads reachable. Nothing may crash on one, and
    nothing may treat one as data."""

    def test_replay_refuses_rather_than_reconstructing(self, engine):
        from src.execution.replay import ReplayRefused, BookReplay

        class _Row:
            market_ticker = "M-1"
            msg_type = "delta"
            sid = 1
            seq = 1
            payload = None

        replay = BookReplay("M-1")
        with pytest.raises(ReplayRefused):
            replay.apply(_Row())

    def test_load_tape_skips_a_nulled_row_instead_of_raising(self, engine):
        """Trade payloads are never nulled, so this is defence in depth — but
        a crash here would take down a trading cycle."""
        from src.execution.fill_sim import load_tape

        with get_session(engine) as session:
            session.add(OrderbookDeltaRaw(
                market_ticker="M-1", msg_type="trade", ts_ms=1000,
                payload=None, received_at=NOW,
            ))
            session.commit()

        assert load_tape(engine, "M-1", 0, 99999) == []

    def test_a_compacted_delta_is_never_counted_as_a_trade_print(self, engine):
        """day-7 measures trade-through rates; a nulled delta must not become
        a phantom print."""
        from src.execution.day7 import measure

        _row(engine, "delta", days_ago=30)
        compact_tape(engine, plan_compaction(engine, now=NOW), now=NOW)

        assert measure(engine) == {}
