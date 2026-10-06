import datetime as dt

from src.session_watchdog import verdict

D = dt.date(2026, 10, 7)


def at(h, m=0):
    return dt.datetime.combine(D, dt.time(h, m), tzinfo=dt.timezone.utc)


def test_no_running_session_inside_the_window_alerts():
    assert "NO MARKET SESSION" in verdict(at(16, 5), [])


def test_a_finished_run_is_not_a_running_session():
    """A link that crashed at 15:20 is 'completed'. Nothing is trading."""
    assert verdict(at(16, 5), [{"status": "completed", "conclusion": "failure"}])


def test_a_running_or_queued_link_is_fine():
    assert verdict(at(16, 5), [{"status": "in_progress"}]) is None
    assert verdict(at(16, 5), [{"status": "queued"}]) is None


def test_outside_the_window_there_is_nothing_to_alert():
    assert verdict(at(22), []) is None
    assert verdict(at(9), []) is None
