"""The orderbook recorder (Phase 3 prerequisite, 2026-08-11).

Kalshi serves no historical book — the trade tape reaches back ~60 days, the
book reaches back zero — so this cannot be backfilled and runs before the
simulator that consumes it.

Every test here has a demonstrated failure mode, per the "a check that cannot
fail is not a check" principle in tasks/lessons.md: each asserts the recorder
does something specific AND that the assertion breaks when that behaviour is
removed (gaps recorded, snapshots kept, sequence reset on reconnect).
"""
from __future__ import annotations

import datetime as dt

import asyncio
import json

import pytest

from src.database import Base, get_session
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap
from src.models.position import Position
from src.recorder.book_recorder import BookRecorder, markets_to_record


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _delta(seq, sid=1, ticker="KXHIGHNY-26AUG12-T90", price="0.4500", delta="1.5"):
    return {
        "type": "orderbook_delta", "sid": sid, "seq": seq,
        "msg": {
            "market_ticker": ticker, "price_dollars": price,
            "delta_fp": delta, "side": "yes", "ts_ms": 1786471379310 + seq,
        },
    }


def _rows(engine, msg_type=None):
    with get_session(engine) as s:
        q = s.query(OrderbookDeltaRaw)
        if msg_type:
            q = q.filter_by(msg_type=msg_type)
        return [
            {"seq": r.seq, "type": r.msg_type, "ticker": r.market_ticker,
             "price": r.price_dollars, "delta": r.delta_fp, "payload": r.payload}
            for r in q.order_by(OrderbookDeltaRaw.id).all()
        ]


class TestPersistence:
    def test_deltas_are_stored_with_the_raw_payload(self, engine):
        rec = BookRecorder(engine, None, ["KXHIGHNY-26AUG12-T90"])
        rec.handle(_delta(1))
        rec._flush()

        rows = _rows(engine)
        assert len(rows) == 1
        assert rows[0]["seq"] == 1
        assert rows[0]["price"] == 0.45
        assert rows[0]["delta"] == 1.5
        # Raw payload survives verbatim: reconstruction logic will change, and
        # re-deriving from the original message must stay possible.
        assert json.loads(rows[0]["payload"])["msg"]["side"] == "yes"

    def test_snapshots_are_kept_not_just_deltas(self, engine):
        """A delta stream without its anchoring snapshot reconstructs nothing."""
        rec = BookRecorder(engine, None, ["M"])
        rec.handle({
            "type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": "M", "yes_dollars": [["0.44", "100"]]},
        })
        rec._flush()
        assert [r["type"] for r in _rows(engine)] == ["snapshot"]

    def test_trades_are_recorded_too(self, engine):
        """The fill rule keys on the tape; the book only proves we were resting."""
        rec = BookRecorder(engine, None, ["M"])
        rec.handle({
            "type": "trade", "sid": 2, "seq": 1,
            "msg": {"market_ticker": "M", "yes_price_dollars": "0.46",
                    "count_fp": "5.04", "taker_outcome_side": "no"},
        })
        rec._flush()
        rows = _rows(engine, "trade")
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"])["msg"]["count_fp"] == "5.04"

    def test_control_frames_are_not_recorded_as_market_data(self, engine):
        rec = BookRecorder(engine, None, ["M"])
        rec.handle({"type": "subscribed", "id": 1, "msg": {"channel": "trade", "sid": 1}})
        rec.handle({"type": "error", "msg": {"code": 8}})
        rec._flush()
        assert _rows(engine) == []

    def test_buffer_flushes_automatically(self, engine):
        from src.recorder.book_recorder import FLUSH_EVERY

        rec = BookRecorder(engine, None, ["M"])
        for seq in range(1, FLUSH_EVERY + 1):
            rec.handle(_delta(seq))
        assert len(_rows(engine)) == FLUSH_EVERY      # flushed without an explicit call


class TestSequenceGaps:
    def test_contiguous_sequence_records_no_gap(self, engine):
        rec = BookRecorder(engine, None, ["M"])
        for seq in (1, 2, 3, 4):
            rec.handle(_delta(seq))
        rec._flush()
        with get_session(engine) as s:
            assert s.query(OrderbookGap).count() == 0

    def test_missing_sequence_is_recorded(self, engine):
        """A gap means the book cannot be reconstructed across it — so a fill
        simulated over that interval must not be trusted."""
        rec = BookRecorder(engine, None, ["M"])
        rec.handle(_delta(1))
        rec.handle(_delta(5))
        rec._flush()

        with get_session(engine) as s:
            gap = s.query(OrderbookGap).one()
            assert gap.expected_seq == 2
            assert gap.received_seq == 5
            assert gap.missing == 3
        assert rec.stats.gaps == 1

    def test_gap_is_recorded_not_repaired(self, engine):
        """Stitching the sequence back together would hide the one thing the
        gap is there to tell us."""
        rec = BookRecorder(engine, None, ["M"])
        rec.handle(_delta(1))
        rec.handle(_delta(5))
        rec._flush()
        assert [r["seq"] for r in _rows(engine)] == [1, 5]   # no synthesised 2-4

    def test_sequences_are_tracked_per_subscription(self, engine):
        """seq is per-sid; comparing across channels would invent gaps."""
        rec = BookRecorder(engine, None, ["M"])
        rec.handle(_delta(1, sid=1))
        rec.handle(_delta(1, sid=2))
        rec.handle(_delta(2, sid=1))
        rec.handle(_delta(2, sid=2))
        rec._flush()
        with get_session(engine) as s:
            assert s.query(OrderbookGap).count() == 0


class TestMarketSelection:
    """Every candidate needs a live `markets` row.

    These used to seed positions and opportunities with no market row at all,
    which production never produces — a scored or held market was ingested by
    definition. `markets_to_record` now refuses a candidate whose liveness it
    cannot establish, because taping settled books is what inflated the day-7
    clock, so the fixtures are brought up to production shape.
    """

    @staticmethod
    def _live_market(session, market_id):
        session.add(Market(
            market_id=market_id, title="t", category="Weather",
            close_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
            status="active",
        ))

    def test_open_positions_come_first(self, engine):
        with get_session(engine) as s:
            self._live_market(s, "HELD")
            self._live_market(s, "SCORED")
            s.add(Position(market_id="HELD", side="yes", entry_price=50,
                           quantity=1, current_price=50, status="open"))
            s.add(Opportunity(
                market_id="SCORED", p_model=0.6, implied_prob=0.5, edge=0.1,
                net_ev=0.05, recommended_side="yes", confidence=0.8,
                status="qualifying", model_name="X",
            ))
            s.commit()
        assert markets_to_record(engine)[0] == "HELD"

    def test_a_held_market_survives_the_cap(self, engine):
        """Exposure matters more than watching."""
        with get_session(engine) as s:
            self._live_market(s, "HELD")
            for i in range(30):
                self._live_market(s, f"S{i}")
                s.add(Opportunity(
                    market_id=f"S{i}", p_model=0.6, implied_prob=0.5, edge=0.1,
                    net_ev=0.05 + i, recommended_side="yes", confidence=0.8,
                    status="qualifying", model_name="X",
                ))
            s.add(Position(market_id="HELD", side="yes", entry_price=50,
                           quantity=1, current_price=50, status="open"))
            s.commit()
        assert "HELD" in markets_to_record(engine, limit=3)

    def test_rejected_markets_are_not_recorded(self, engine):
        with get_session(engine) as s:
            s.add(Opportunity(
                market_id="REJ", p_model=0.6, implied_prob=0.5, edge=0.1,
                net_ev=0.05, recommended_side="yes", confidence=0.8,
                status="rejected", model_name="X",
            ))
            s.commit()
        assert markets_to_record(engine) == []

    def test_no_duplicates(self, engine):
        with get_session(engine) as s:
            self._live_market(s, "BOTH")
            s.add(Position(market_id="BOTH", side="yes", entry_price=50,
                           quantity=1, current_price=50, status="open"))
            s.add(Opportunity(
                market_id="BOTH", p_model=0.6, implied_prob=0.5, edge=0.1,
                net_ev=0.05, recommended_side="yes", confidence=0.8,
                status="qualifying", model_name="X",
            ))
            s.commit()
        assert markets_to_record(engine) == ["BOTH"]


class _FakeSocket:
    """Yields scripted frames, then raises to simulate a drop."""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.sent = []
        self._current = []

    def __call__(self):
        return self

    async def __aenter__(self):
        self._current = list(self._scripts.pop(0)) if self._scripts else []
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self._current:
            raise ConnectionError("socket closed")
        return json.dumps(self._current.pop(0))


class TestConnection:
    def test_subscribes_to_both_channels_for_the_markets(self, engine):
        socket = _FakeSocket([[_delta(1)]])
        rec = BookRecorder(engine, None, ["A", "B"], connect=socket)
        asyncio.run(rec.run(duration_seconds=0.3))

        channels = [c["params"]["channels"][0] for c in socket.sent]
        assert "orderbook_delta" in channels and "trade" in channels
        assert socket.sent[0]["params"]["market_tickers"] == ["A", "B"]

    def test_reconnects_and_resubscribes_after_a_drop(self, engine):
        socket = _FakeSocket([[_delta(1)], [_delta(1), _delta(2)]])
        rec = BookRecorder(engine, None, ["A"], connect=socket, reconnect_delay=0.01)
        asyncio.run(rec.run(duration_seconds=0.6))

        assert rec.stats.reconnects >= 1
        subscribes = [c for c in socket.sent if c["cmd"] == "subscribe"]
        assert len(subscribes) >= 4          # both channels, both connections

    def test_reconnect_does_not_manufacture_a_gap(self, engine):
        """seq restarts per subscription, so carrying the old counter across a
        reconnect would log a gap on every single drop."""
        socket = _FakeSocket([[_delta(7)], [_delta(1), _delta(2)]])
        rec = BookRecorder(engine, None, ["A"], connect=socket, reconnect_delay=0.01)
        asyncio.run(rec.run(duration_seconds=0.6))

        with get_session(engine) as s:
            assert s.query(OrderbookGap).count() == 0

    def test_data_is_flushed_when_the_connection_drops(self, engine):
        """Buffered messages must not die with the socket."""
        socket = _FakeSocket([[_delta(1), _delta(2)], []])
        rec = BookRecorder(engine, None, ["A"], connect=socket, reconnect_delay=0.01)
        asyncio.run(rec.run(duration_seconds=0.5))
        assert len(_rows(engine)) == 2

    def test_no_markets_means_no_connection_attempt(self, engine):
        socket = _FakeSocket([[_delta(1)]])
        rec = BookRecorder(engine, None, [], connect=socket)
        stats = asyncio.run(rec.run(duration_seconds=0.2))
        assert stats.written == 0
        assert socket.sent == []
