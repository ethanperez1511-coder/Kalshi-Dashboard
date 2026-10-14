"""db-stats and cut-tape are never blocked by a lagging schema.

They are what an operator reaches for when the database is in trouble, and
a database in trouble is exactly when a migration may not have landed (at
Neon's cap, even ALTER TABLE can be refused). So the maintenance workflow
applies pending additive migrations first, with the same `python -m
src.migrate` step the cycle runs, and does not stop if that step fails. The
two emergency actions then run against whatever schema exists. Every other
action still refuses a lagging schema: they write through the models.

The lag here is real, not stubbed: today's own column, dropped.
"""
from __future__ import annotations

import runpy
import sys

import pytest
import yaml
from sqlalchemy import text

from src.database import Base


@pytest.fixture
def lagging(db_engine):
    import src.models  # noqa: F401
    Base.metadata.create_all(db_engine)
    with db_engine.begin() as conn:
        conn.execute(text("ALTER TABLE db_size_samples DROP COLUMN tables_json"))
    return db_engine


def _run(argv, engine, monkeypatch, capsys):
    import src.config
    import src.database

    monkeypatch.setattr(src.config, "require_production_database", lambda url: None)
    monkeypatch.setattr(src.database, "get_engine", lambda url=None, *a, **k: engine)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://stub/stub")
    monkeypatch.delenv("MIGRATE_ON_BOOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["src.maintenance", *argv])
    monkeypatch.delitem(sys.modules, "src.maintenance.__main__", raising=False)
    try:
        runpy.run_module("src.maintenance", run_name="__main__")
        code = 0
    except SystemExit as exit_signal:
        code = exit_signal.code if exit_signal.code is not None else 0
    return code, capsys.readouterr().out


def test_the_schema_really_lags(lagging):
    from src.database import pending_schema_changes
    assert any("tables_json" in p for p in pending_schema_changes(lagging))


def test_db_stats_runs_on_a_lagging_schema(lagging, monkeypatch, capsys):
    code, out = _run(["--db-stats"], lagging, monkeypatch, capsys)
    assert code in (0, 1), out          # 1 = empty census, not a refusal
    assert "SchemaOutOfDate" not in out


def test_cut_tape_dry_run_runs_on_a_lagging_schema(lagging, monkeypatch, capsys):
    code, out = _run(["--cut-tape", "--days", "7"], lagging, monkeypatch, capsys)
    assert code == 0, out
    assert "CUT TAPE" in out.upper()


def test_other_actions_still_refuse_a_lagging_schema(lagging, monkeypatch, capsys):
    from src.database import SchemaOutOfDate
    with pytest.raises(SchemaOutOfDate):
        _run(["--clear-halt"], lagging, monkeypatch, capsys)


def _steps():
    wf = yaml.safe_load(open(".github/workflows/maintenance.yml"))
    return wf["jobs"]["run"]["steps"]


def test_the_workflow_migrates_before_any_action():
    steps = _steps()
    names = [s.get("run", "") for s in steps]
    migrate = next(i for i, r in enumerate(names) if r.strip() == "python -m src.migrate")
    action = next(i for i, r in enumerate(names) if "src.maintenance" in r)
    assert migrate < action
    step = steps[migrate]
    assert step["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
    # A failed migration must not block db-stats or cut-tape.
    assert step.get("continue-on-error") is True


def test_it_is_the_same_migrator_the_cycle_runs():
    cycle = yaml.safe_load(open(".github/workflows/trade.yml"))
    runs = [s.get("run", "").strip() for job in cycle["jobs"].values() for s in job["steps"]]
    assert "python -m src.migrate" in runs


def test_retention_migrates_before_it_prunes():
    wf = yaml.safe_load(open(".github/workflows/retention.yml"))
    runs = [s.get("run", "").strip() for s in wf["jobs"]["prune"]["steps"]]
    migrate = runs.index("python -m src.migrate")
    prune = next(i for i, r in enumerate(runs) if "src.maintenance.prune" in r)
    assert migrate < prune
