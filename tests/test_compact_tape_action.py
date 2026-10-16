"""Emergency compaction past the 95% line, bounded by measured file growth.

2026-10-16: the file sat above 95% of the cap with 822k payloads (~241 MB)
unnulled, and retention's compaction refused at the 95% line every run. The
risk compaction poses is extending the file, and that is measured directly
per batch, so this action ignores the line and instead stops the moment the
file has grown 1 MB, in 2,000-row batches with VACUUM after each. It runs
only with the COMPACT-TAPE token. Retention's own compaction is unchanged.
"""
from __future__ import annotations

import datetime as dt

import pytest

import src.maintenance.tape as tape
from src.database import Base, get_engine, get_session
from src.maintenance.retention import NEON_CAP_BYTES
from src.models.orderbook_raw import OrderbookDeltaRaw

MB = 1_000_000
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def tape_engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(e)
    with get_session(e) as s:
        for i in range(10):
            s.add(OrderbookDeltaRaw(market_ticker="M", msg_type="delta", sid=1, seq=i,
                                    payload="x" * 100, received_at=NOW - dt.timedelta(days=3)))
        s.commit()
    return e


def _nulled(e):
    with get_session(e) as s:
        return s.query(OrderbookDeltaRaw).filter(OrderbookDeltaRaw.payload.is_(None)).count()


def test_emergency_mode_runs_above_the_95_percent_line(tape_engine, monkeypatch):
    monkeypatch.setattr(tape, "database_size_bytes", lambda e: int(0.99 * NEON_CAP_BYTES))
    monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
    plan = tape.plan_compaction(tape_engine)
    result = tape.compact_tape(tape_engine, plan, **tape.EMERGENCY)
    assert result["nulled"] == 10 and not result["stopped"]


def test_emergency_mode_stops_at_one_megabyte_of_file_growth(tape_engine, monkeypatch):
    sizes = iter([990 * MB, 990 * MB, int(991.2 * MB)] + [int(991.2 * MB)] * 20)
    monkeypatch.setattr(tape, "database_size_bytes", lambda e: next(sizes))
    monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
    monkeypatch.setitem(tape.EMERGENCY, "batch", 3)
    result = tape.compact_tape(tape_engine, tape.plan_compaction(tape_engine), **tape.EMERGENCY)
    assert "file grew" in result["stopped"]
    assert result["nulled"] == 3


def test_the_emergency_settings():
    assert tape.EMERGENCY["file_growth_limit"] == 1 * MB
    assert tape.EMERGENCY["batch"] == 2_000


def test_retention_compaction_has_no_file_line_and_a_2_mb_bound(tape_engine, monkeypatch):
    """Ruling 2026-10-16 (step 4): the 95% line is replaced by ~2 MB of file
    growth per run."""
    assert not hasattr(tape, "CAP_STOP_FRACTION")
    assert tape.FILE_GROWTH_LIMIT_BYTES == 2 * MB
    sizes = iter([1050 * MB, 1050 * MB, int(1052.1 * MB)] + [int(1052.1 * MB)] * 20)
    monkeypatch.setattr(tape, "database_size_bytes", lambda e: next(sizes))
    monkeypatch.setattr(tape, "tape_index_bytes", lambda e: 100 * MB)
    monkeypatch.setattr(tape, "BATCH", 3)
    result = tape.compact_tape(tape_engine, tape.plan_compaction(tape_engine))
    assert result["nulled"] == 3 and "file grew" in result["stopped"]


def _run(argv, engine, monkeypatch, capsys):
    from tests.test_maintenance_entrypoint import run_module
    return run_module(argv, engine, monkeypatch, capsys)


def test_without_the_token_it_is_a_dry_run(tape_engine, monkeypatch, capsys):
    code, out = _run(["--compact-tape"], tape_engine, monkeypatch, capsys)
    assert code == 0, out
    assert "DRY RUN" in out and _nulled(tape_engine) == 0


def test_a_wrong_token_changes_nothing(tape_engine, monkeypatch, capsys):
    code, out = _run(["--compact-tape", "--confirm", "COMPACT"], tape_engine, monkeypatch, capsys)
    assert code == 2 and _nulled(tape_engine) == 0
    assert "COMPACT-TAPE" in out          # refused by the action, not by argparse


def test_with_the_token_it_nulls(tape_engine, monkeypatch, capsys):
    code, out = _run(["--compact-tape", "--confirm", "COMPACT-TAPE"], tape_engine, monkeypatch, capsys)
    assert code == 0, out
    assert _nulled(tape_engine) == 10


def test_the_workflow_offers_it():
    import yaml
    wf = yaml.safe_load(open(".github/workflows/maintenance.yml"))
    assert "compact-tape" in wf[True]["workflow_dispatch"]["inputs"]["action"]["options"]
    assert any("--compact-tape" in s.get("run", "") for s in wf["jobs"]["run"]["steps"])
