"""A failure page must name the job that failed, and say what the failure means.

2026-10-12 08:15: the RETENTION workflow failed and paged "LIVE CHECKS FAILED
— an upstream contract may have changed", because the shared alert step's
default text was the live-checks text and retention set no text of its own.
And on 10-10/11 live-checks itself paged that text when its suite never ran at
all (collection error, pytest exit 2): the opposite of what it claimed.
"""
from src.alert_live_failure import alert_text


def test_another_workflow_never_pages_as_live_checks():
    text = alert_text({"GITHUB_WORKFLOW": "retention"})
    assert "RETENTION FAILED" in text and "LIVE CHECKS" not in text


def test_a_failed_live_check_is_the_contract_warning():
    text = alert_text({"GITHUB_WORKFLOW": "live-checks", "ALERT_KIND": "live-checks", "LIVE_EXIT": "1"})
    assert "LIVE CHECKS FAILED" in text and "contract" in text


def test_a_suite_that_could_not_run_says_so():
    for code in ("2", "4", "5", ""):
        text = alert_text({"GITHUB_WORKFLOW": "live-checks", "ALERT_KIND": "live-checks", "LIVE_EXIT": code})
        assert "COULD NOT RUN" in text and "NOT verified" in text, code


def test_an_explicit_text_wins():
    assert alert_text({"ALERT_TEXT": "custom", "GITHUB_WORKFLOW": "x"}) == "custom"
