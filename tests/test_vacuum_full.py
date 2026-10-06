"""The one-off VACUUM FULL gate, and the retention verdict it exists to clear.

Both entry points run through runpy as __main__, the way the workflows invoke
them (L27). VACUUM FULL takes an exclusive lock and, near the cap, can be
refused space for its own copy. So the token gate is tested as strictly as the
other destructive actions, and the space guard is tested on the numbers that
actually apply today.
"""
from __future__ import annotations

import runpy
import sys

import pytest

import src.maintenance.retention as retention
from src.maintenance.retention import NEON_CAP_BYTES, STORAGE_BUDGET_BYTES
from src.maintenance.vacuum_full import (
    CONFIRM_TOKEN,
    TableStat,
    VacuumPlan,
    execute_vacuum,
    refused_tables,
)
from tests.test_maintenance_entrypoint import run_module, seeded  # noqa: F401

MB = 1024 * 1024


class TestTokenGate:
    def test_no_token_is_a_dry_run(self, seeded, monkeypatch, capsys):
        code, out = run_module(["--vacuum-full"], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "VACUUM FULL" in out

    def test_a_near_miss_token_changes_nothing(self, seeded, monkeypatch, capsys):
        code, _ = run_module(
            ["--vacuum-full", "--confirm", "VACUUM-FULL"], seeded, monkeypatch, capsys,
        )
        assert code == 2

    @pytest.mark.parametrize("other", [
        "PURGE-ORPHAN-MARKETS", "CLOSE-LEGACY-POSITIONS", "RETIRE-DEPLOY-SHA",
    ])
    def test_another_actions_token_does_not_authorise_it(
        self, seeded, monkeypatch, capsys, other,
    ):
        code, _ = run_module(
            ["--vacuum-full", "--confirm", other], seeded, monkeypatch, capsys,
        )
        assert code == 2

    def test_its_token_does_not_authorise_a_purge(self, seeded, monkeypatch, capsys):
        code, _ = run_module(
            ["--purge-markets", "--confirm", CONFIRM_TOKEN], seeded, monkeypatch, capsys,
        )
        assert code == 2


class TestSpaceGuard:
    def test_a_table_that_fits_in_the_headroom_is_allowed(self):
        size = 721 * MB
        tape = TableStat("orderbook_delta_raw", 250 * MB, 0, 0)
        assert refused_tables(size, [tape]) == []

    def test_a_copy_larger_than_the_headroom_is_refused(self):
        """Today, before pruning: if the tape were 400 MB of a 721 MB
        database, a full copy would cross the cap mid-rewrite."""
        size = 721 * MB
        tape = TableStat("orderbook_delta_raw", 400 * MB, 0, 0)
        assert refused_tables(size, [tape]) == ["orderbook_delta_raw"]

    def test_execute_never_touches_a_refused_table(self, db_engine):
        plan = VacuumPlan(
            supported=True, size_before=NEON_CAP_BYTES - MB,
            tables=[TableStat("orderbook_delta_raw", 500 * MB, 0, 0)],
            refused=["orderbook_delta_raw"],
        )
        assert execute_vacuum(db_engine, plan) == {}


def _run_prune(engine, monkeypatch, size_bytes):
    """`python -m src.maintenance.prune` as __main__, with the measured size
    fixed. A SQLite test DB is a few KB and cannot show a budget breach."""
    import src.config
    import src.database

    monkeypatch.setattr(src.config, "require_production_database", lambda url: None)
    monkeypatch.setattr(src.database, "get_engine", lambda url=None, *a, **k: engine)
    monkeypatch.setattr(src.database, "verify_or_migrate", lambda *a, **k: None)
    monkeypatch.setattr(retention, "database_size_bytes", lambda e: size_bytes)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://stub/stub")
    monkeypatch.setattr(sys, "argv", ["src.maintenance.prune"])
    monkeypatch.delitem(sys.modules, "src.maintenance.prune", raising=False)
    try:
        runpy.run_module("src.maintenance.prune", run_name="__main__")
        return 0
    except SystemExit as exit_signal:
        return exit_signal.code or 0


class TestRetentionVerdict:
    def test_under_budget_is_green(self, seeded, monkeypatch):
        assert _run_prune(seeded, monkeypatch, STORAGE_BUDGET_BYTES - MB) == 0

    def test_over_budget_is_red_while_far_below_the_cap(self, seeded, monkeypatch):
        """Red at 600 MB, 59% of the cap. The old code went red against a
        stale 512 MiB tier and then stayed red for 26 days, until red meant
        nothing. Red now means over OUR budget, with headroom left to act."""
        assert _run_prune(seeded, monkeypatch, 600 * MB) == 1

    def test_the_old_tier_constant_no_longer_judges_anything(self, seeded, monkeypatch):
        """470 MB was 92% of the 512 MiB tier, so the old code exited 1.
        It is under budget."""
        assert _run_prune(seeded, monkeypatch, 470 * MB) == 0
