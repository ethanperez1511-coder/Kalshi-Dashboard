"""The recorder must survive a dead connection and must not record corpses.

Two production failures, one run, 2026-08-16:

  psycopg.OperationalError: consuming input failed: SSL connection has been
  closed unexpectedly    (on INSERT INTO orderbook_delta_raw)

Neon closes idle connections. Weather books are quiet, the recorder holds one
pooled connection across a 55-minute window, and between two sparse flushes the
connection dies. Three things then compounded: the buffer was cleared BEFORE the
insert, so the batch was lost even in principle; the exception escaped `_flush`
into the reconnect handler, which calls `_flush` again and raised a second time;
and that second raise left `run()` entirely, ending the hour. A dropped
connection must cost one batch at worst, never the run.

The same run was subscribed to KXHIGH*-26AUG13 contracts on 2026-08-16 —
markets that settled three days earlier. `markets_to_record` read Opportunity
rows and nothing ever invalidates them, so the recorder spent the hour taping
books that do not exist. That is not merely wasted: it inflates the day-7
coverage clock with hours that contain no live market.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

from src.database import Base, get_session
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.models.position import Position
from src.recorder.book_recorder import BookRecorder, markets_to_record

NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(engine, market_id, close_offset_days, status="active", category="Weather"):
    with get_session(engine) as session:
        session.add(Market(
            market_id=market_id, title="t", category=category,
            close_date=NOW + dt.timedelta(days=close_offset_days), status=status,
        ))
        session.commit()


def _opportunity(engine, market_id, status="qualifying", net_ev=0.05, age_hours=0.0):
    with get_session(engine) as session:
        session.add(Opportunity(
            market_id=market_id, model_name="WeatherModel", p_model=0.5,
            implied_prob=0.5, edge=0.05, net_ev=net_ev, confidence=0.8,
            status=status, recommended_side="yes",
            scored_at=NOW - dt.timedelta(hours=age_hours),
        ))
        session.commit()


def _message(ticker="KXHIGHNY-26AUG17-T91", seq=1):
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {"market_ticker": ticker, "side": "yes", "price_dollars": "0.42"},
    }


# ---------------------------------------------------------------------------
# Bug 1 — a dropped connection costs one batch, never the run
# ---------------------------------------------------------------------------

class _Boom(Exception):
    """Stands in for psycopg.OperationalError on a closed SSL connection."""


class TestFlushSurvivesADeadConnection:
    def test_flush_does_not_raise_when_every_attempt_fails(self, engine, monkeypatch):
        recorder = BookRecorder(engine, auth=None, markets=["A-1"])
        recorder.handle(_message())

        monkeypatch.setattr(
            "src.recorder.book_recorder.get_session",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("SSL connection closed")),
        )

        recorder._flush()          # must not raise — this ended the hour in prod

    def test_a_transient_failure_is_retried_and_the_batch_survives(
        self, engine, monkeypatch,
    ):
        """The batch was cleared before the insert, so a retry had nothing to
        retry. It is dropped only once it is durable."""
        recorder = BookRecorder(engine, auth=None, markets=["A-1"])
        recorder.handle(_message())

        real = __import__(
            "src.recorder.book_recorder", fromlist=["get_session"]
        ).get_session
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Boom("SSL connection closed")
            return real(*args, **kwargs)

        monkeypatch.setattr("src.recorder.book_recorder.get_session", flaky)
        monkeypatch.setattr("src.recorder.book_recorder.RETRY_SLEEP", 0.0)

        recorder._flush()

        with get_session(engine) as session:
            assert session.execute(
                select(OrderbookDeltaRaw.market_ticker)
            ).scalars().all() == ["KXHIGHNY-26AUG17-T91"]
        assert recorder.stats.written == 1

    def test_a_permanently_dead_connection_is_counted_not_hidden(
        self, engine, monkeypatch,
    ):
        """Losing a batch is acceptable; losing it silently is not."""
        recorder = BookRecorder(engine, auth=None, markets=["A-1"])
        recorder.handle(_message())

        monkeypatch.setattr(
            "src.recorder.book_recorder.get_session",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("SSL connection closed")),
        )
        monkeypatch.setattr("src.recorder.book_recorder.RETRY_SLEEP", 0.0)

        recorder._flush()

        assert recorder.stats.write_failures == 1
        assert recorder.stats.messages_lost == 1
        assert recorder.stats.written == 0

    def test_the_buffer_does_not_grow_without_bound_after_repeated_failures(
        self, engine, monkeypatch,
    ):
        """A recorder that buffers a dead hour into RAM is the Railway failure."""
        recorder = BookRecorder(engine, auth=None, markets=["A-1"])
        monkeypatch.setattr(
            "src.recorder.book_recorder.get_session",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("SSL connection closed")),
        )
        monkeypatch.setattr("src.recorder.book_recorder.RETRY_SLEEP", 0.0)

        for seq in range(1, 12):
            recorder.handle(_message(seq=seq))
            recorder._flush()

        assert recorder._buffer == []


class TestGapWriteSurvivesADeadConnection:
    def test_recording_a_gap_does_not_kill_the_run(self, engine, monkeypatch):
        recorder = BookRecorder(engine, auth=None, markets=["A-1"])
        recorder.handle(_message(seq=1))

        monkeypatch.setattr(
            "src.recorder.book_recorder.get_session",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("SSL connection closed")),
        )
        monkeypatch.setattr("src.recorder.book_recorder.RETRY_SLEEP", 0.0)

        recorder.handle(_message(seq=9))       # a gap: expected 2, got 9


class TestRunSurvivesADeadConnection:
    def test_a_failing_flush_inside_the_reconnect_handler_does_not_escape(
        self, engine, monkeypatch,
    ):
        """The exact shape of the production crash.

        The socket drops; the handler flushes; the flush raises because the DB
        connection is also gone; that second exception is outside the try and
        takes the whole run with it.
        """
        monkeypatch.setattr(
            "src.recorder.book_recorder.get_session",
            lambda *a, **k: (_ for _ in ()).throw(_Boom("SSL connection closed")),
        )
        monkeypatch.setattr("src.recorder.book_recorder.RETRY_SLEEP", 0.0)

        class _Socket:
            async def send(self, _):
                return None

            async def recv(self):
                raise ConnectionError("socket dropped")

        class _Conn:
            async def __aenter__(self):
                return _Socket()

            async def __aexit__(self, *exc):
                return False

        recorder = BookRecorder(
            engine, auth=None, markets=["A-1"], connect=_Conn, reconnect_delay=0.0,
        )

        stats = asyncio.run(recorder.run(duration_seconds=0.2))

        assert stats.reconnects >= 1


# ---------------------------------------------------------------------------
# Bug 2 — never subscribe to a market that has already closed
# ---------------------------------------------------------------------------

class TestSubscribeListExcludesCorpses:
    def test_a_settled_market_is_not_recorded(self, engine):
        """KXHIGHCHI-26AUG13 was still being taped on 2026-08-16."""
        _market(engine, "KXHIGHCHI-26AUG13-T76", close_offset_days=-3)
        _opportunity(engine, "KXHIGHCHI-26AUG13-T76")

        assert markets_to_record(engine, now=NOW) == []

    def test_a_live_market_is_recorded(self, engine):
        _market(engine, "KXHIGHNY-26AUG17-T91", close_offset_days=1)
        _opportunity(engine, "KXHIGHNY-26AUG17-T91")

        assert markets_to_record(engine, now=NOW) == ["KXHIGHNY-26AUG17-T91"]

    def test_status_closed_is_excluded_even_with_a_future_close_date(self, engine):
        """Both conditions independently: an early settlement closes a market
        before its published close time."""
        _market(engine, "EARLY-1", close_offset_days=5, status="finalized")
        _opportunity(engine, "EARLY-1")

        assert markets_to_record(engine, now=NOW) == []

    def test_an_opportunity_with_no_market_row_is_not_subscribed(self, engine):
        """Unknown liveness is not a licence to record. The blank-ticker rows in
        production came from exactly this class of subscription."""
        _opportunity(engine, "GHOST-1")

        assert markets_to_record(engine, now=NOW) == []

    def test_a_stale_opportunity_on_a_live_market_is_dropped(self, engine):
        """Opportunity rows are never invalidated, so recency is the only thing
        that makes the list refresh per run rather than accumulate forever."""
        _market(engine, "OLD-1", close_offset_days=5)
        _market(engine, "NEW-1", close_offset_days=5)
        _opportunity(engine, "OLD-1", age_hours=72)
        _opportunity(engine, "NEW-1", age_hours=0.5)

        assert markets_to_record(engine, now=NOW) == ["NEW-1"]

    def test_held_positions_still_come_first(self, engine):
        _market(engine, "HELD-1", close_offset_days=2)
        _market(engine, "WATCH-1", close_offset_days=2)
        _opportunity(engine, "WATCH-1", net_ev=0.99)
        with get_session(engine) as session:
            session.add(Position(
                market_id="HELD-1", side="yes", entry_price=40,
                quantity=1, current_price=40, status="open",
            ))
            session.commit()

        assert markets_to_record(engine, now=NOW)[0] == "HELD-1"

    def test_a_held_position_in_a_closed_market_is_not_recorded(self, engine):
        """There is no book to record on a market that has closed, however
        exposed we are to it. Settlement reads the exchange, never this feed."""
        _market(engine, "HELD-DEAD", close_offset_days=-1)
        with get_session(engine) as session:
            session.add(Position(
                market_id="HELD-DEAD", side="yes", entry_price=40,
                quantity=1, current_price=40, status="open",
            ))
            session.commit()

        assert markets_to_record(engine, now=NOW) == []
