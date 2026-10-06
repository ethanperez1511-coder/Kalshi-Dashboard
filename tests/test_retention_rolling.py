"""The tape retention window rolls with the clock. It is never anchored.

The September outage was set up by this. Delta protection was anchored to the
FIRST recorded delta: `window_end = min(received_at) + 60 days`. That window
does two wrong things, and both are tested here.

  * Before day 60 it protects everything, so the tape grows without bound.
    At the measured +49.5 MB/day, sixty days is ~3 GB against a 1 GiB cap.
  * On day 60 it deletes every delta older than the window end in a single
    statement. That includes the last fourteen days, which replay and shadow
    actually read. It then re-anchors on whatever survived and protects the
    next sixty days. The result is a sawtooth that overshoots the cap and
    then wipes the tape.

The ruling (2026-10-06) is a rolling window of DELTA_RETENTION_DAYS measured
back from NOW. These tests only call the public retention entry point, so they
ran against the anchored implementation unchanged. They failed there before
they passed here.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.maintenance.retention import apply_retention, database_size_bytes
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)

# The ruling. Restated, not imported: if the constant drifts, this should fail.
RULED_DAYS = 14


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _tape(engine, when, n=1, payload="{}"):
    with get_session(engine) as s:
        for i in range(n):
            s.add(OrderbookDeltaRaw(
                market_ticker="KXHIGHNY-26OCT12-T70", msg_type="delta",
                sid=1, seq=i, payload=payload, received_at=when,
            ))
        s.commit()


def _ages(engine, now):
    with get_session(engine) as s:
        stamps = [r for (r,) in s.query(OrderbookDeltaRaw.received_at).all()]
    out = []
    for t in stamps:
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        out.append((now - t).total_seconds() / 86400)
    return sorted(out)


def test_the_anchored_wipe_does_not_take_the_replay_window(engine):
    """This is the day the anchored window closes. The first delta is 61 days
    old, so the anchored code deletes everything older than yesterday. The
    fourteen days that replay and shadow read must survive it."""
    for age in range(61, -1, -1):
        _tape(engine, NOW - dt.timedelta(days=age))

    apply_retention(engine, now=NOW)

    ages = _ages(engine, NOW)
    assert len(ages) == RULED_DAYS + 1, ages        # ages 0..14 inclusive
    assert max(ages) <= RULED_DAYS


def test_old_tape_is_pruned_while_newer_tape_exists(engine):
    """The anchored window protected a 30-day-old delta because the tape was
    younger than 60 days. The rolling window does not care how old the
    oldest row is."""
    _tape(engine, NOW - dt.timedelta(days=30))
    _tape(engine, NOW - dt.timedelta(days=RULED_DAYS + 1))
    _tape(engine, NOW)

    apply_retention(engine, now=NOW)

    assert _ages(engine, NOW) == [0.0]


def test_tape_inside_the_window_is_never_pruned(engine):
    _tape(engine, NOW - dt.timedelta(days=RULED_DAYS - 1))
    _tape(engine, NOW - dt.timedelta(hours=1))

    apply_retention(engine, now=NOW)

    assert len(_ages(engine, NOW)) == 2


def test_daily_retention_holds_a_plateau_and_never_sawtooths(engine):
    """Ninety simulated days, one prune per day, the way the cron runs it.
    Row count must settle at the window and stay there. The anchored code
    climbs to sixty, wipes to one, then climbs again."""
    start = NOW - dt.timedelta(days=90)
    counts = []
    for day in range(90):
        today = start + dt.timedelta(days=day)
        _tape(engine, today, n=5)
        apply_retention(engine, now=today)
        with get_session(engine) as s:
            counts.append(s.query(OrderbookDeltaRaw).count())

    settled = counts[RULED_DAYS + 1:]
    assert max(settled) == min(settled) == 5 * (RULED_DAYS + 1), counts
    # Never wiped: every day after warm-up still holds the full window.
    assert all(c >= 5 * RULED_DAYS for c in counts[RULED_DAYS:])


def test_size_settles_once_the_window_is_full(engine):
    """Measured bytes, not rows. Freed pages are reused, so the file stops
    growing once the window is full. Postgres does the same with plain
    VACUUM, which prune runs after every pass. Under the anchored code the
    file keeps growing until day 60."""
    payload = "x" * 2000
    start = NOW - dt.timedelta(days=60)
    sizes = []
    for day in range(60):
        today = start + dt.timedelta(days=day)
        _tape(engine, today, n=20, payload=payload)
        apply_retention(engine, now=today)
        sizes.append(database_size_bytes(engine))

    at_full_window = sizes[RULED_DAYS + 2]
    assert max(sizes[RULED_DAYS + 2:]) <= at_full_window * 1.10, sizes
