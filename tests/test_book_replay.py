"""Book replay: what it reconstructs, and the four things it refuses.

A book rebuilt from an incomplete stream looks exactly like one rebuilt from a
complete stream. Nothing downstream can tell them apart, so every refusal here
is load-bearing and each one is tested by feeding it the corruption it exists
to catch.

Phase 3 scaffolding. Nothing here reads the day-7 window or computes a capture
estimate.
"""
from __future__ import annotations

import json

import pytest

from src.execution.replay import (
    BookReplay,
    ReplayRefused,
    load_rows,
    replay,
)


class _Row:
    def __init__(self, msg_type, seq, payload, sid=1, ts_ms=1_000):
        self.msg_type = msg_type
        self.seq = seq
        self.sid = sid
        self.ts_ms = ts_ms
        self.payload = json.dumps(payload)


def _snapshot(seq=1, sid=1, yes=None, no=None, ts_ms=1_000):
    return _Row("snapshot", seq, {"msg": {
        "market_ticker": "KXHIGHNY-26AUG13-T92",
        "yes": yes if yes is not None else [[40, 100], [39, 250]],
        "no": no if no is not None else [[59, 80], [58, 300]],
    }}, sid, ts_ms)


def _delta(seq, side="yes", price=40, change=-25, sid=1, ts_ms=1_001):
    return _Row("delta", seq, {"msg": {
        "market_ticker": "KXHIGHNY-26AUG13-T92",
        "side": side, "price": price, "delta": change,
    }}, sid, ts_ms)


# --------------------------------------------------------------------------
# 1. It reconstructs
# --------------------------------------------------------------------------

class TestReconstruction:
    def test_a_snapshot_establishes_the_book(self):
        engine = BookReplay("M")
        state = engine.apply(_snapshot())
        assert state.best("yes") == (40, 100)
        assert state.best("no") == (59, 80)
        assert state.total("yes") == 350

    def test_deltas_move_resting_size(self):
        engine = BookReplay("M")
        engine.apply(_snapshot())
        state = engine.apply(_delta(2, "yes", 40, -25))
        assert state.depth_at("yes", 40) == 75
        state = engine.apply(_delta(3, "yes", 40, +10))
        assert state.depth_at("yes", 40) == 85

    def test_a_level_emptied_to_zero_moves_the_touch_down(self):
        engine = BookReplay("M")
        engine.apply(_snapshot())
        state = engine.apply(_delta(2, "yes", 40, -100))
        assert state.depth_at("yes", 40) == 0
        assert state.best("yes") == (39, 250), "an empty level is not the touch"

    def test_a_new_price_level_appears(self):
        engine = BookReplay("M")
        engine.apply(_snapshot())
        state = engine.apply(_delta(2, "yes", 41, +60))
        assert state.best("yes") == (41, 60)

    def test_prices_in_dollars_are_read_as_cents(self):
        """The newer schema quotes dollars. 0.40 is 40c, not 0."""
        engine = BookReplay("M")
        engine.apply(_snapshot(yes=[[0.40, 100]], no=[[0.59, 80]]))
        assert engine.state().best("yes") == (40, 100)

    def test_a_trade_print_does_not_move_the_book(self):
        """Prints and resting size are different facts. The exchange sends the
        corresponding delta separately, and applying both would double-count."""
        engine = BookReplay("M")
        engine.apply(_snapshot())
        before = engine.state().levels
        assert engine.apply(_Row("trade", 2, {"msg": {"count": 10}})) is None
        assert engine.state().levels == before
        assert engine.trades_seen == 1


# --------------------------------------------------------------------------
# 2. It refuses — one test per way the reconstruction becomes unprovable
# --------------------------------------------------------------------------

class TestRefusals:
    def test_a_sequence_gap_stops_the_replay(self):
        engine = BookReplay("M")
        engine.apply(_snapshot(seq=1))
        engine.apply(_delta(2))
        with pytest.raises(ReplayRefused, match="sequence gap"):
            engine.apply(_delta(4))          # 3 never arrived

    def test_a_delta_before_any_snapshot_is_refused(self):
        engine = BookReplay("M")
        with pytest.raises(ReplayRefused, match="nothing to apply it to"):
            engine.apply(_delta(1))

    def test_a_delta_driving_a_level_negative_is_refused(self):
        """Sequence numbers say nothing was missed; arithmetic says otherwise.
        Clamping to zero would destroy the only evidence that the stream is
        incomplete."""
        engine = BookReplay("M")
        engine.apply(_snapshot())
        with pytest.raises(ReplayRefused, match="a message was missed"):
            engine.apply(_delta(2, "yes", 40, -500))    # only 100 resting

    def test_an_unparseable_payload_is_refused(self):
        engine = BookReplay("M")
        row = _snapshot()
        row.payload = "{not json"
        with pytest.raises(ReplayRefused, match="not JSON"):
            engine.apply(row)

    def test_a_price_outside_zero_to_one_hundred_is_refused(self):
        """A unit mix-up scales the whole book by 100. Guessing which
        convention produced a 4000c level is not a resolution."""
        engine = BookReplay("M")
        with pytest.raises(ReplayRefused, match="out of range"):
            engine.apply(_snapshot(yes=[[4000, 100]]))

    def test_a_delta_with_no_readable_side_is_refused(self):
        engine = BookReplay("M")
        engine.apply(_snapshot())
        bad = _delta(2)
        bad.payload = json.dumps({"msg": {"price": 40, "delta": -5}})
        with pytest.raises(ReplayRefused, match="no readable side"):
            engine.apply(bad)

    def test_a_negative_quantity_in_a_snapshot_is_refused(self):
        engine = BookReplay("M")
        with pytest.raises(ReplayRefused, match="negative quantity"):
            engine.apply(_snapshot(yes=[[40, -5]]))


# --------------------------------------------------------------------------
# 3. Reconnects are not gaps
# --------------------------------------------------------------------------

class TestReconnect:
    def test_a_new_sid_restarts_numbering_without_a_gap(self):
        """`seq` is per-subscription. Comparing across a resubscribe would
        manufacture a gap on every reconnect — the recorder reported 1
        reconnect and 0 gaps, and those must stay separate numbers."""
        engine = BookReplay("M")
        engine.apply(_snapshot(seq=1, sid=1))
        engine.apply(_delta(2, sid=1))
        engine.apply(_snapshot(seq=1, sid=2))     # resubscribed, seq restarts
        assert engine.state().best("yes") == (40, 100)

    def test_a_reconnect_invalidates_the_book_until_a_new_snapshot(self):
        """Nothing says the two subscriptions saw the same state."""
        engine = BookReplay("M")
        engine.apply(_snapshot(seq=1, sid=1))
        assert engine.has_base is True
        with pytest.raises(ReplayRefused, match="nothing to apply it to"):
            engine.apply(_delta(1, sid=2))


# --------------------------------------------------------------------------
# 4. Coverage is reported, never absorbed
# --------------------------------------------------------------------------

class TestCoverage:
    def test_a_gap_costs_its_window_not_the_whole_recording(self):
        rows = [
            _snapshot(seq=1), _delta(2, change=-10),
            _delta(5, change=-10),          # gap: 3 and 4 missing
            _snapshot(seq=6), _delta(7, change=-10),
        ]
        states, coverage = replay(rows, "M")
        assert coverage.refusals == ["sequence gap: expected 3, got 5"]
        # The gap costs delta 5 alone. The snapshot after it re-establishes a
        # base and replay continues: 4 of the 5 messages still reconstruct.
        assert coverage.states == 4
        assert coverage.messages_applied == 4, (
            "counts describe the recording, not the engine instance — resetting "
            "them on refusal under-reports coverage by the work already done"
        )
        assert "REFUSED" in coverage.summary()

    def test_a_clean_run_reports_no_refusals(self):
        rows = [_snapshot(seq=1)] + [_delta(i, change=-1) for i in range(2, 8)]
        states, coverage = replay(rows, "M")
        assert coverage.refusals == []
        assert coverage.states == 7
        assert "REFUSED" not in coverage.summary()

    def test_stop_on_refusal_returns_one_provably_continuous_run(self):
        rows = [_snapshot(seq=1), _delta(2), _delta(9), _delta(10)]
        states, coverage = replay(rows, "M", stop_on_refusal=True)
        assert coverage.states == 2
        assert coverage.refused == 1


# --------------------------------------------------------------------------
# 5. Loading preserves the evidence of a gap
# --------------------------------------------------------------------------

def test_rows_load_in_insertion_order_not_sequence_order(db_engine):
    """Ordering by seq would renumber a gap out of existence. Insertion order
    is what the recorder saw."""
    from src.database import Base, get_session
    from src.models.orderbook_raw import OrderbookDeltaRaw

    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        for seq in (1, 2, 5):
            s.add(OrderbookDeltaRaw(
                market_ticker="M", msg_type="delta", sid=1, seq=seq,
                ts_ms=1000 + seq, payload="{}",
            ))
        s.commit()

    assert [r.seq for r in load_rows(db_engine, "M")] == [1, 2, 5]
