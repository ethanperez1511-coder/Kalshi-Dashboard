"""Ruling 2026-10-06 (C): near the cap, give up the oldest tape a day at a time.

Pre-authorised: from 85% of the cap, retention may cut the tape window one day
at a time, oldest first, down to a 7-day floor, alerting on every step.
Hitting the cap is worse than losing the oldest day.

One refinement, stated rather than hidden: a step is taken only while LIVE
data is at or above 75% of the cap. Deleting rows does not shrink the file
(plain VACUUM marks the space reusable), so when the file is high because of
dead pages, deleting tape costs data and buys nothing. That case gets an alert
to dispatch shrink_tape and no deletion.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.maintenance.retention as retention
from src.database import Base, get_session
from src.maintenance.retention import NEON_CAP_BYTES, defend_cap
from src.models.orderbook_raw import OrderbookDeltaRaw

NOW = dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone.utc)
CAP = NEON_CAP_BYTES


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        for age in range(14):                      # one row per day, ages 0..13
            s.add(OrderbookDeltaRaw(
                market_ticker="M", msg_type="delta", sid=1, seq=age, payload="{}",
                received_at=NOW - dt.timedelta(days=age, hours=1),
            ))
        s.commit()
    return db_engine


def _ages(engine):
    with get_session(engine) as s:
        return sorted(
            (NOW - (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))).days
            for (t,) in s.query(OrderbookDeltaRaw.received_at).all()
        )


def _sizes(monkeypatch, engine, file_fraction, live_per_row):
    """File size fixed; live data proportional to tape rows left."""
    monkeypatch.setattr(retention, "database_size_bytes", lambda e: int(file_fraction * CAP))

    def live(e):
        with get_session(engine) as s:
            return s.query(OrderbookDeltaRaw).count() * live_per_row

    monkeypatch.setattr(retention, "live_bytes_estimate", live)


def test_below_eighty_five_percent_nothing_happens(engine, monkeypatch):
    _sizes(monkeypatch, engine, 0.84, live_per_row=0.07 * CAP)
    alerts = []
    assert defend_cap(engine, NOW, alerts.append) == []
    assert len(_ages(engine)) == 14 and alerts == []


def test_it_cuts_the_oldest_day_first_and_alerts_each_step(engine, monkeypatch):
    # 14 rows x 6% = 84% live. Cut to 12 days -> 72%: two steps.
    _sizes(monkeypatch, engine, 0.90, live_per_row=0.06 * CAP)
    alerts = []

    steps = defend_cap(engine, NOW, alerts.append)

    assert len(steps) == 2
    assert _ages(engine) == list(range(12))       # ages 12 and 13 gone, oldest first
    assert len(alerts) == 2 and all("tape window" in a for a in alerts)


def test_it_never_goes_below_the_seven_day_floor(engine, monkeypatch):
    _sizes(monkeypatch, engine, 0.95, live_per_row=0.5 * CAP)   # never gets under 75%
    alerts = []

    defend_cap(engine, NOW, alerts.append)

    assert _ages(engine) == list(range(7))
    assert any("floor" in a for a in alerts)


def test_dead_pages_get_a_shrink_alert_not_a_deletion(engine, monkeypatch):
    """File high, live low: the realistic case after a big prune."""
    _sizes(monkeypatch, engine, 0.88, live_per_row=0.02 * CAP)  # 28% live
    alerts = []

    assert defend_cap(engine, NOW, alerts.append) == []
    assert len(_ages(engine)) == 14
    assert len(alerts) == 1 and "shrink_tape" in alerts[0]
