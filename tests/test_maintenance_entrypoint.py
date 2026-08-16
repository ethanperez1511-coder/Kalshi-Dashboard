"""Run the maintenance module the way the workflow runs it.

`_retire` was defined BELOW `if __name__ == "__main__": sys.exit(main())`. Under
`python -m src.maintenance` the interpreter reaches the guard and calls main()
before it ever binds that name, so the dispatcher raised NameError on the first
real dry run. `retire_deploy.py` had eighteen passing tests and the wiring
between it and the entry point had none.

The tests that missed it imported the module — which executes the whole file,
including everything after the guard — and then called `main()`. That is not
what the workflow does, and the difference is exactly the bug.

So every test here goes through `runpy.run_module(..., run_name="__main__")`,
which reproduces the real execution order: the module body runs top to bottom
and main() is invoked from inside it. Argument combinations are every pair the
workflow can dispatch.
"""
from __future__ import annotations

import runpy
import sys

import pytest

from src.database import Base, get_session
from src.legacy_cutoff import gate_count
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings
from src.models.trade import Trade

BAD_SHA = "e807f8dd0024530f41edf27088c6f4e4f883450b"


@pytest.fixture
def seeded(db_engine):
    """A production-shaped DB: settings, one retirable trade, one open position."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(TradingSettings(bankroll=100.0, paper_trade_count=1))
        s.add(Trade(
            market_id="KXHIGHCHI-26AUG13-T76", side="no", action="buy",
            price=92, quantity=3, p_model=0.0571, implied_prob=0.09,
            edge=-0.0329, traded_edge=0.0229, evaluated_price=92, net_ev=0.02,
            position_size_dollars=2.76, confidence=0.85, reasoning="t",
            is_paper=True, status="filled", model_name="WeatherModel",
            deploy_sha=BAD_SHA, is_legacy=False,
        ))
        s.add(Position(
            market_id="KXHIGHCHI-26AUG13-T76", side="no", entry_price=92,
            quantity=3, current_price=92, status="open",
        ))
        s.add(PriceSnapshot(
            market_id="KXHIGHCHI-26AUG13-T76", yes_bid=5, yes_ask=7,
            last_price=6, volume=500,
        ))
        s.commit()
    return db_engine


def run_module(argv, engine, monkeypatch, capsys):
    """Execute `python -m src.maintenance <argv>` in-process, as __main__.

    Patches are applied to the SOURCE modules rather than to an already-imported
    `src.maintenance.__main__`, because runpy re-executes the file: its
    `from src.database import get_engine` binds whatever is there at that
    moment.
    """
    import src.config
    import src.database

    monkeypatch.setattr(src.config, "require_production_database", lambda url: None)
    monkeypatch.setattr(src.database, "get_engine", lambda url=None, *a, **k: engine)
    monkeypatch.setattr(src.database, "verify_or_migrate", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://stub/stub")
    monkeypatch.setattr(sys, "argv", ["src.maintenance", *argv])
    monkeypatch.delitem(sys.modules, "src.maintenance.__main__", raising=False)

    try:
        runpy.run_module("src.maintenance", run_name="__main__")
        code = 0
    except SystemExit as exit_signal:
        code = exit_signal.code if exit_signal.code is not None else 0
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------

def test_the_module_runs_as_main_with_a_retire_argument(seeded, monkeypatch, capsys):
    """The failing dispatch, reproduced. Calling main() after importing the
    module passes even when the module is broken, because the import binds the
    names the guard never reaches."""
    code, out = run_module(["--retire-sha", "e807f8dd"], seeded, monkeypatch, capsys)
    assert code == 0, out
    assert "RETIRE DEPLOY" in out


def _entry_modules():
    import pathlib

    return sorted(
        path for path in pathlib.Path("src").rglob("*.py")
        if 'if __name__ == "__main__"' in path.read_text()
    )


def test_every_entry_module_has_its_guard_last():
    """The class of bug, across the whole tree — not just the one that bit.

    Anything after `if __name__ == "__main__": sys.exit(main())` is unreachable
    when the module is RUN and perfectly reachable when it is IMPORTED. A test
    suite only ever imports, so the two look identical from inside it. That is
    what let a NameError reach production with eighteen green tests around the
    function that was missing.
    """
    import ast

    modules = _entry_modules()
    assert modules, "no entry modules found — the scan is broken, not the code"

    offenders = {}
    for path in modules:
        body = ast.parse(path.read_text()).body
        guard = next(
            i for i, node in enumerate(body)
            if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
        )
        trailing = [getattr(n, "name", type(n).__name__) for n in body[guard + 1:]]
        if trailing:
            offenders[str(path)] = trailing

    assert offenders == {}, (
        f"defined after the __main__ guard, so unreachable under `python -m`: "
        f"{offenders}"
    )


# --------------------------------------------------------------------------
# Every argument pair the workflow can dispatch
# --------------------------------------------------------------------------

class TestWorkflowArgumentMatrix:
    """The workflow has two free-text inputs. That is five reachable
    combinations and the dispatcher branches on all of them."""

    def test_blank_blank_is_the_legacy_reconcile_dry_run(self, seeded, monkeypatch, capsys):
        code, out = run_module(["--confirm", ""], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "PRODUCTION TRADE RECORD" in out
        assert gate_count(seeded) == 1, "a dry run must change nothing"

    def test_sha_blank_is_the_retire_dry_run(self, seeded, monkeypatch, capsys):
        code, out = run_module(
            ["--retire-sha", "e807f8dd", "--confirm", ""], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "DRY RUN" in out
        assert "1 -> 0" in out
        assert gate_count(seeded) == 1, "a dry run must change nothing"
        with get_session(seeded) as s:
            assert s.query(Position).one().status == "open"

    def test_sha_with_the_right_token_executes(self, seeded, monkeypatch, capsys):
        code, out = run_module(
            ["--retire-sha", "e807f8dd", "--confirm", "RETIRE-DEPLOY-SHA"],
            seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "EXECUTED" in out
        assert gate_count(seeded) == 0
        with get_session(seeded) as s:
            assert s.query(Position).one().status == "closed"
            assert s.query(TradingSettings).first().paper_trade_count == 0

    def test_sha_with_a_wrong_token_exits_two_and_changes_nothing(
        self, seeded, monkeypatch, capsys,
    ):
        code, out = run_module(
            ["--retire-sha", "e807f8dd", "--confirm", "RETIRE-DEPLOY"],
            seeded, monkeypatch, capsys)
        assert code == 2
        assert gate_count(seeded) == 1
        with get_session(seeded) as s:
            assert s.query(Position).one().status == "open"

    def test_the_unwind_token_does_not_authorise_a_retirement(
        self, seeded, monkeypatch, capsys,
    ):
        code, _ = run_module(
            ["--retire-sha", "e807f8dd", "--confirm", "CLOSE-LEGACY-POSITIONS"],
            seeded, monkeypatch, capsys)
        assert code == 2
        assert gate_count(seeded) == 1

    def test_blank_sha_with_the_unwind_token_runs_the_legacy_path(
        self, seeded, monkeypatch, capsys,
    ):
        """No retire_sha input means the workflow takes the other branch. It
        must not fall through into a retirement."""
        code, out = run_module(
            ["--confirm", "CLOSE-LEGACY-POSITIONS"], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "RETIRE DEPLOY" not in out

    def test_a_blank_retire_sha_input_takes_the_legacy_branch(
        self, seeded, monkeypatch, capsys,
    ):
        """The workflow shell guards on `-n "$RETIRE_SHA"`, but the CLI must be
        safe on its own: an empty --retire-sha is not a wildcard."""
        code, out = run_module(
            ["--retire-sha", "", "--confirm", ""], seeded, monkeypatch, capsys)
        assert code == 0, out
        assert "PRODUCTION TRADE RECORD" in out
        assert gate_count(seeded) == 1


# --------------------------------------------------------------------------
# The read-only census, dispatched the way the workflow dispatches it
# --------------------------------------------------------------------------

def test_db_stats_runs_as_main_and_needs_no_token(seeded, monkeypatch, capsys):
    """`--db-stats` is the branch reached while something is on fire.

    It carries no confirmation token, so the only thing standing between it and
    a destructive action is the dispatch order in main(). That order is what
    this executes.
    """
    code, out = run_module(["--db-stats"], seeded, monkeypatch, capsys)

    assert code == 0, out
    assert "DB CENSUS" in out
    assert "open markets" in out.lower()


def test_db_stats_ignores_a_confirmation_token_entirely(seeded, monkeypatch, capsys):
    """A census must never become an execution because a token was left in the
    box from the previous run. The workflow checks db_stats first; so does
    main(), and this pins that both do."""
    code, out = run_module(
        ["--db-stats", "--confirm", "CLOSE-LEGACY-POSITIONS"], seeded, monkeypatch, capsys,
    )

    assert code == 0, out
    assert "DB CENSUS" in out
    # The legacy-position report never ran, so nothing was closed.
    assert "would close" not in out.lower()
    with get_session(seeded) as s:
        assert s.query(Position).filter_by(status="open").count() == 1


def test_db_stats_leaves_the_gate_count_untouched(seeded, monkeypatch, capsys):
    before = gate_count(seeded)
    run_module(["--db-stats"], seeded, monkeypatch, capsys)
    assert gate_count(seeded) == before
