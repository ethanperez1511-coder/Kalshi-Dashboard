"""The recorder runs 15:00-21:00 UTC. Silence outside that is the plan.

2026-10-12: the digest said "nothing recorded for 5.5h — recorder may be
down" at ~02:30 UTC, when the recorder had stopped at 21:00 as scheduled.
A warning that is usually wrong teaches everyone to ignore the one time it is
right. It now warns only when the recorder is silent DURING a session, or
stopped well before the last session closed.
"""
import datetime as dt

from src.recorder.health import recording_status

D = dt.date(2026, 10, 12)


def at(h, m=0, day=D):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=dt.timezone.utc)


def test_the_overnight_gap_after_a_full_session_is_expected():
    last = at(20, 59, D - dt.timedelta(days=1))
    status = recording_status(last, now=at(2, 30))
    assert status.startswith("ℹ️") and "outside the session" in status


def test_silence_during_the_session_is_down():
    assert recording_status(at(15, 40), now=at(17, 0)).startswith("⚠️")


def test_a_session_that_stopped_early_is_flagged_overnight():
    last = at(17, 10, D - dt.timedelta(days=1))
    assert recording_status(last, now=at(2, 30)).startswith("⚠️")


def test_recent_messages_during_the_session_say_nothing_alarming():
    assert recording_status(at(16, 55), now=at(17, 0)) == ""
