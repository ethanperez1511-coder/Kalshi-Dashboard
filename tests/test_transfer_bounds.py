"""No query may fetch a whole growing table across the wire.

Neon cut every connection on 2026-08-23: monthly DATA TRANSFER quota exceeded.
Production stopped completely — cycles, recorder, live-checks.

The cause was one query. `recorder_health` selected `(market_ticker,
received_at)` for EVERY row of `orderbook_delta_raw` so it could classify each
row live/dead in Python, and `deployment_state` calls it once per cycle —
outside the daily-heartbeat block. At 650,000 rows that is ~39 MB per call,
288 calls a day, **11.2 GB/day**, growing linearly with the tape. Nothing
capped it because every existing test bounded QUERY COUNT and none bounded
BYTES.

The distinction that matters for a transfer quota: rows SCANNED are free —
the server does that work — and rows RETURNED are what crosses the wire. An
aggregate over ten million rows costs one row of transfer. A bare column
select over ten million rows costs ten million.

So this pins the class: any statement against a table that grows without bound
must aggregate, or filter, or limit. It may never be a bare projection.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import event

from src.database import Base, get_engine, get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.timezone.utc)

# Tables whose row count grows with uptime and can never be fetched whole.
UNBOUNDED_TABLES = ("orderbook_delta_raw", "price_snapshots")


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'transfer.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def captured(engine):
    """Every SQL statement this engine executes."""
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "after_cursor_execute", _record)
    yield statements
    event.remove(engine, "after_cursor_execute", _record)


def _is_bounded(sql: str) -> bool:
    """A SELECT is bounded if it aggregates, filters, or limits."""
    lowered = sql.lower()
    return any(
        marker in lowered
        for marker in ("count(", "max(", "min(", "sum(", "avg(",
                       "group by", "where", "limit", "distinct")
    )


def _offenders(statements) -> list:
    out = []
    for sql in statements:
        lowered = sql.lower()
        if not lowered.startswith("select"):
            continue
        if not any(table in lowered for table in UNBOUNDED_TABLES):
            continue
        if not _is_bounded(sql):
            out.append(sql)
    return out


def _seed(engine, markets=6, hours=40, per_hour=25, prefix="A"):
    with get_session(engine) as session:
        for m in range(markets):
            ticker = f"KXHIGHNY-26{prefix}UG2{m}-T90"
            session.add(Market(
                market_id=ticker, title="t", category="Weather",
                close_date=NOW + dt.timedelta(days=1), status="active",
            ))
            for hour in range(hours):
                for i in range(per_hour):
                    session.add(OrderbookDeltaRaw(
                        market_ticker=ticker, msg_type="delta", payload="{}",
                        # Minutes stay INSIDE the hour bucket: offsetting
                        # backwards by minutes would straddle the boundary and
                        # count one extra hour of "coverage".
                        received_at=NOW - dt.timedelta(hours=hour)
                        + dt.timedelta(minutes=i),
                    ))
        session.commit()


class TestRecorderHealthIsBounded:
    def test_it_does_not_fetch_the_whole_tape(self, engine, captured):
        """The exact query that stopped production."""
        from src.recorder.health import recorder_health

        _seed(engine)
        captured.clear()
        recorder_health(engine, now=NOW)

        assert _offenders(captured) == [], (
            "an unbounded projection over a growing table is 39 MB per call "
            "at production scale"
        )

    def test_rows_returned_do_not_scale_with_row_count(self, engine, captured):
        """Bounded by (markets x hours), not by messages.

        Ten times the messages in the same markets and hours must not move the
        wire cost, which is the property that makes this survive a year of
        recording.
        """
        from src.recorder.health import recorder_health

        _seed(engine, markets=3, hours=5, per_hour=2)
        captured.clear()
        recorder_health(engine, now=NOW)
        small = len(captured)

        with get_session(engine) as session:
            for _ in range(200):
                session.add(OrderbookDeltaRaw(
                    market_ticker="KXHIGHNY-26AUG20-T90", msg_type="delta",
                    payload="{}", received_at=NOW - dt.timedelta(hours=1),
                ))
            session.commit()

        captured.clear()
        recorder_health(engine, now=NOW)

        assert len(captured) == small

    def test_the_numbers_are_unchanged_by_the_rewrite(self, engine):
        """Cheaper must still mean correct."""
        from src.recorder.health import recorder_health

        _seed(engine, markets=2, hours=3, per_hour=4)
        health = recorder_health(engine, now=NOW)

        assert health["messages"] == 2 * 3 * 4
        assert health["markets"] == 2
        assert health["liveness"]["live"] == 24
        assert health["liveness"]["dead"] == 0
        assert health["per_category"]["Weather"]["hours"] == 3

    def test_scope_bucketing_still_applies(self, engine):
        from src.recorder.health import recorder_health

        _seed(engine, markets=2, hours=2, per_hour=2)
        health = recorder_health(
            engine, now=NOW, scope_of=lambda ticker, category: f"S:{ticker[:8]}",
        )

        assert "S:KXHIGHNY" in health["per_category"]


class TestTheClassNotTheInstance:
    """Every read-only reporting path, not just the one that bit."""

    def test_day7_measure_is_bounded(self, engine, captured):
        from src.execution.day7 import measure

        _seed(engine)
        captured.clear()
        measure(engine)

        assert _offenders(captured) == []

    def test_db_stats_is_bounded(self, engine, captured):
        from src.maintenance.db_stats import collect

        _seed(engine)
        captured.clear()
        collect(engine, now=NOW)

        assert _offenders(captured) == []


class TestThePerCyclePathIsFlat:
    """Per-cycle cost must not grow with how long the recorder has run.

    Even aggregated, grouping per (market, hour) grows with uptime — a month of
    recording would creep back toward the outage. `deployment_state` runs every
    cycle, so it takes the pulse instead: scalar aggregates only.
    """

    def test_pulse_cost_is_independent_of_hours_recorded(self, engine, captured):
        from src.recorder.health import recorder_pulse

        _seed(engine, markets=2, hours=3, per_hour=2)
        captured.clear()
        recorder_pulse(engine, now=NOW)
        short = len(captured)

        _seed(engine, markets=2, hours=200, per_hour=2, prefix="B")
        captured.clear()
        recorder_pulse(engine, now=NOW)

        assert len(captured) == short

    def test_deployment_state_does_not_group_per_hour(self, engine, captured):
        """The per-cycle caller. This is the query that stopped production."""
        from src.deployment_state import deployment_state

        _seed(engine)
        captured.clear()
        deployment_state(engine)

        assert _offenders(captured) == []
        grouped_by_hour = [
            s for s in captured
            if "orderbook_delta_raw" in s.lower() and "group by" in s.lower()
        ]
        assert grouped_by_hour == [], (
            "a per-(market, hour) grouping on the per-cycle path grows with "
            "uptime and walks back into the same outage"
        )

    def test_pulse_still_reports_liveness(self, engine):
        from src.recorder.health import recorder_pulse

        _seed(engine, markets=2, hours=2, per_hour=3)
        pulse = recorder_pulse(engine, now=NOW)

        assert pulse["messages"] == 12
        assert pulse["markets"] == 2
        assert pulse["hours_since_last_message"] is not None
