"""The backstop starts the session when the session's own triggers did not.

None of market-session's five daily cron triggers fired on its first two days
(2026-10-06/07), while the */15 backstop was delivered several times a day. A
backstop cycle that lands inside the window with no session link running
dispatches one, using the workflow's own token (no external credential).
"""
from __future__ import annotations

import datetime as dt

from src.session_kick import should_kick

D = dt.date(2026, 10, 8)


def at(h, m=0):
    return dt.datetime.combine(D, dt.time(h, m), tzinfo=dt.timezone.utc)


def test_inside_the_window_with_no_session_it_kicks():
    assert should_kick(at(16, 48), runs=[])


def test_a_crashed_link_is_restarted():
    assert should_kick(at(17), runs=[{"status": "completed", "conclusion": "failure"}])


def test_a_running_or_queued_link_is_left_alone():
    assert not should_kick(at(16), runs=[{"status": "in_progress"}])
    assert not should_kick(at(16), runs=[{"status": "queued"}])


def test_outside_the_window_it_never_kicks():
    for t in (at(9), at(12, 59), at(20, 41), at(23)):
        assert not should_kick(t, runs=[]), t


def test_a_kick_before_the_open_is_fine_the_link_waits():
    """13:00-15:00: the link starts and sleeps to 15:00, as a cron trigger would."""
    assert should_kick(at(13, 30), runs=[])
