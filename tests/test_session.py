"""The daily market session: when a link waits, runs, chains or exits.

GitHub cron starts runs 4-8 hours late and drops some (measured 2026-10-06),
so the session cannot trust any single trigger. Each link decides from the
clock alone what to do, and a link that cannot cover the whole window hands
the rest to a successor it dispatches itself.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.session import (
    LINK_SECONDS,
    SESSION_END,
    SESSION_START,
    plan_link,
    run_session,
)

D = dt.date(2026, 10, 7)


def at(h, m=0):
    return dt.datetime.combine(D, dt.time(h, m), tzinfo=dt.timezone.utc)


class TestPlanLink:
    def test_the_window_is_the_ruled_one(self):
        assert (SESSION_START, SESSION_END) == (dt.time(15, 0), dt.time(21, 0))

    def test_an_early_trigger_waits_for_the_open_then_chains(self):
        """Started 13:00: may run until 18:40 (5h40m), so it opens at 15:00,
        runs to 18:40 and dispatches a successor for the rest."""
        plan = plan_link(at(13), started=at(13))
        assert plan.action == "run"
        assert plan.wait_until == at(15)
        assert plan.run_until == at(13) + dt.timedelta(seconds=LINK_SECONDS)
        assert plan.chain is True

    def test_even_a_trigger_at_the_open_must_chain(self):
        """The window is 6 h and a hosted job lives 5h40m usable: every day
        is at least two links. The successor covers 20:40–21:00."""
        plan = plan_link(at(15), started=at(15))
        assert plan.run_until == at(20, 40) and plan.chain is True
        tail = plan_link(at(20, 42), started=at(20, 42))
        assert tail.run_until == at(21) and tail.chain is False

    def test_a_late_trigger_runs_what_is_left(self):
        plan = plan_link(at(19, 30), started=at(19, 30))
        assert plan.wait_until is None
        assert plan.run_until == at(21) and plan.chain is False

    def test_after_the_close_it_exits(self):
        """The duplicate trigger queued behind a finished session lands here."""
        assert plan_link(at(21, 5), started=at(21, 5)).action == "exit"

    def test_a_trigger_too_early_to_reach_the_open_hands_off_at_once(self):
        """Started 09:00 it could only run to 14:40, before the open. It
        dispatches a successor instead of sleeping out its own clock."""
        plan = plan_link(at(9), started=at(9))
        assert plan.action == "chain_now"

    def test_never_runs_past_the_hosted_job_limit(self):
        for start_h in range(10, 21):
            plan = plan_link(at(start_h), started=at(start_h))
            if plan.action == "run":
                assert plan.run_until - at(start_h) <= dt.timedelta(seconds=LINK_SECONDS)


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


class TestRunSession:
    def _run(self, start, **kw):
        clock = _Clock(start)
        cycles, recorders, chained = [], [], []

        def cycle():
            cycles.append(clock.now)
            clock.sleep(180)               # a cycle takes ~3 min
            return 0

        def recorder(seconds):
            recorders.append((clock.now, seconds))
            return 0

        code = run_session(
            now=clock, sleep=clock.sleep, run_cycle=cycle,
            start_recorder=recorder, recorder_done=lambda: True,
            dispatch_successor=lambda: chained.append(clock.now) or True,
            alert=lambda msg: None, **kw,
        )
        return code, cycles, recorders, chained

    def test_a_link_runs_a_cycle_every_fifteen_minutes(self):
        code, cycles, _, chained = self._run(at(15))
        assert code == 0
        assert len(cycles) == 23                       # 15:00 .. 20:30
        assert cycles[0] == at(15) and cycles[1] == at(15, 15)
        assert cycles[-1] == at(20, 30)
        assert chained == [at(20, 40)]

    def test_the_tail_link_covers_the_last_cycle_of_the_day(self):
        """Picks up the day's grid: 20:30 ran in the first link, so the tail
        runs 20:45 and nothing else."""
        code, cycles, _, chained = self._run(at(20, 42))
        assert cycles == [at(20, 45)] and chained == []

    def test_a_late_link_joins_the_grid(self):
        _, cycles, _, _ = self._run(at(19, 32))
        assert cycles[0] == at(19, 45)

    def test_no_cycle_runs_outside_the_window(self):
        _, cycles, _, _ = self._run(at(13))
        assert all(at(15) <= c < at(21) for c in cycles)

    def test_the_recorder_is_restarted_hourly_to_refresh_subscriptions(self):
        _, _, recorders, _ = self._run(at(15))
        assert all(seconds <= 3600 for _, seconds in recorders)
        assert len(recorders) >= 6

    def test_an_early_link_dispatches_its_successor(self):
        _, _, _, chained = self._run(at(13))
        assert len(chained) == 1

    def test_a_failed_cycle_alerts_and_fails_the_link_but_the_session_continues(self):
        clock = _Clock(at(15))
        alerts, cycles = [], []

        def cycle():
            cycles.append(clock.now)
            clock.sleep(60)
            return 1 if len(cycles) == 2 else 0

        code = run_session(
            now=clock, sleep=clock.sleep, run_cycle=cycle,
            start_recorder=lambda s: 0, recorder_done=lambda: True,
            dispatch_successor=lambda: True, alert=alerts.append,
        )
        assert code == 1
        assert len(cycles) == 23                       # kept going
        assert len(alerts) == 1                        # alerted once, at once

    def test_a_failed_successor_dispatch_is_an_alert_not_a_silence(self):
        clock = _Clock(at(13))
        alerts = []
        code = run_session(
            now=clock, sleep=clock.sleep, run_cycle=lambda: 0,
            start_recorder=lambda s: 0, recorder_done=lambda: True,
            dispatch_successor=lambda: False, alert=alerts.append,
        )
        assert code == 1
        assert any("successor" in a for a in alerts)


@pytest.fixture
def settings_db(db_engine):
    import src.models  # noqa: F401  — register every table before create_all
    from src.database import Base

    Base.metadata.create_all(db_engine)
    return db_engine


class TestHeartbeatOncePerDate:
    """Under '24 h since the last', a heartbeat sent at 20:50 is not due
    again until 20:50 the next day. If no cycle runs between 20:50 and 21:00,
    it slips to the following session and a whole day is skipped."""

    def test_due_on_a_new_utc_date_even_inside_24_hours(self, settings_db):
        db_engine = settings_db
        from src.database import get_session
        from src.models.settings import TradingSettings

        with get_session(db_engine) as s:
            s.add(TradingSettings(bankroll=100.0, last_heartbeat_at=at(20, 50)))
            s.commit()
        tomorrow_open = at(15) + dt.timedelta(days=1)
        assert TradingSettings.heartbeat_due(db_engine, now=tomorrow_open)

    def test_not_due_twice_on_the_same_date(self, settings_db):
        db_engine = settings_db
        from src.database import get_session
        from src.models.settings import TradingSettings

        with get_session(db_engine) as s:
            s.add(TradingSettings(bankroll=100.0, last_heartbeat_at=at(15)))
            s.commit()
        assert not TradingSettings.heartbeat_due(db_engine, now=at(20, 45))
