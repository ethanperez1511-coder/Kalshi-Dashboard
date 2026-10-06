"""Cadence is a number other numbers depend on. Change it deliberately.

Moving from 5-minute to 15-minute cycles (2026-08-24, to fit Neon's free-tier
transfer quota) is not a cron edit. Two snapshot constants were tuned around
the old interval and silently break at the new one:

    SNAPSHOT_HEARTBEAT_MINUTES  how often an UNCHANGED market is re-recorded
    MAX_SNAPSHOT_AGE_MINUTES    how old a snapshot may be and still be scored

At 5 minutes, a market skipped by the heartbeat was at most 20+5=25 minutes old
when the scorer looked, inside the 30-minute guard, and a single missed cycle
still landed at 25. At 15 minutes a missed cycle puts it at 45 — past the guard
— so the market silently drops out of scoring entirely. The failure is not an
error anywhere; the market simply stops being considered.

So the relationship is derived rather than left as two magic numbers that were
consistent once. The invariant:

    MAX_SNAPSHOT_AGE_MINUTES >= SNAPSHOT_HEARTBEAT_MINUTES + 2 * CYCLE_MINUTES

which is heartbeat plus one tolerated missed cycle plus the one in progress.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def config(reload_config):
    def _load(**env):
        return reload_config(**env)

    return _load


class TestTheInvariantHolds:
    def test_shipped_defaults_satisfy_it(self, config):
        cfg = config()

        assert cfg.MAX_SNAPSHOT_AGE_MINUTES >= (
            cfg.SNAPSHOT_HEARTBEAT_MINUTES + 2 * cfg.CYCLE_MINUTES
        )

    def test_one_missed_cycle_does_not_starve_the_scorer(self, config):
        """The regression this exists to prevent."""
        cfg = config()
        worst_age = cfg.SNAPSHOT_HEARTBEAT_MINUTES + 2 * cfg.CYCLE_MINUTES

        assert worst_age <= cfg.MAX_SNAPSHOT_AGE_MINUTES, (
            f"a market skipped by the heartbeat and hit by one missed cycle is "
            f"{worst_age}min old, past the {cfg.MAX_SNAPSHOT_AGE_MINUTES}min "
            f"guard — it would vanish from scoring with no error anywhere"
        )

    def test_it_tracks_a_cadence_change(self, config):
        """Set the cadence, and the tolerance follows without a second edit."""
        cfg = config(TRADING_CYCLE_MINUTES="30")

        assert cfg.MAX_SNAPSHOT_AGE_MINUTES >= 20 + 60

    def test_an_explicit_override_still_wins(self, config):
        cfg = config(TRADING_MAX_SNAPSHOT_AGE_MINUTES="999")

        assert cfg.MAX_SNAPSHOT_AGE_MINUTES == 999


class TestCadenceMatchesTheSession:
    """Cycles run inside the market session (2026-10-06), which takes its
    cadence from configuration. trade.yml keeps a */N BACKSTOP schedule until
    the session has proven itself (L32; exit criterion in tasks/todo.md), and
    that cron must state the same cadence. Only the session records the book."""

    def test_the_session_cadence_is_the_configured_one(self):
        from src import session, trading_config

        assert session.CYCLE_SECONDS == trading_config.CYCLE_MINUTES * 60

    def test_the_backstop_cron_states_the_configured_cadence(self, config):
        import pathlib
        import re

        text = pathlib.Path(".github/workflows/trade.yml").read_text()
        match = re.search(r'cron:\s*"\*/(\d+) \* \* \* \*"', text)
        assert match, "trade.yml backstop schedule missing (L32: keep it until the exit criterion)"
        assert int(match.group(1)) == config().CYCLE_MINUTES

    def test_only_the_session_records_the_book(self):
        import pathlib

        import yaml

        doc = yaml.safe_load(pathlib.Path(".github/workflows/book-recorder.yml").read_text())
        triggers = doc.get("on", doc.get(True))     # YAML 1.1 reads `on` as True
        assert "schedule" not in triggers

    def test_the_job_timeout_still_fits_inside_one_interval(self, config):
        """A job outliving its interval queues the next one behind it."""
        import pathlib
        import re

        cfg = config()
        text = pathlib.Path(".github/workflows/trade.yml").read_text()
        timeout = int(re.search(r"timeout-minutes:\s*(\d+)", text).group(1))

        assert timeout <= cfg.CYCLE_MINUTES


class TestBudgetsFitTheJob:
    def test_stage_budgets_fit_inside_the_job_cap(self, config):
        import pathlib
        import re

        cfg = config()
        text = pathlib.Path(".github/workflows/trade.yml").read_text()
        timeout = int(re.search(r"timeout-minutes:\s*(\d+)", text).group(1))
        budgeted = (cfg.INGEST_BUDGET_SECONDS + cfg.SCORE_BUDGET_SECONDS) / 60.0

        assert budgeted < timeout
