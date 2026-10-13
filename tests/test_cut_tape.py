"""Emergency: cut the tape window by hand, never below the 7-day floor.

For the 95% plan (2026-10-13). Deleting old tape frees space INSIDE the file
for new writes, which is what stops growth toward the cap. Token-gated and
human-typed, like every destructive action.
"""
import datetime as dt

import pytest

from src.database import get_session
from src.models.orderbook_raw import OrderbookDeltaRaw
from tests.test_maintenance_entrypoint import run_module, seeded  # noqa: F401

NOW = dt.datetime.now(dt.timezone.utc)


def _tape(engine):
    with get_session(engine) as s:
        for age in range(10):
            s.add(OrderbookDeltaRaw(market_ticker="M", msg_type="delta", sid=1, seq=age,
                                    payload="{}", received_at=NOW - dt.timedelta(days=age, hours=1)))
        s.commit()


def _count(engine):
    with get_session(engine) as s:
        return s.query(OrderbookDeltaRaw).count()


def test_dry_run_counts_and_deletes_nothing(seeded, monkeypatch, capsys):
    _tape(seeded)
    code, out = run_module(["--cut-tape", "--days", "8"], seeded, monkeypatch, capsys)
    assert code == 0 and "would delete 2" in out and _count(seeded) == 10


def test_the_token_cuts_to_the_window(seeded, monkeypatch, capsys):
    _tape(seeded)
    code, _ = run_module(["--cut-tape", "--days", "8", "--confirm", "CUT-TAPE"], seeded, monkeypatch, capsys)
    assert code == 0 and _count(seeded) == 8


def test_never_below_the_floor(seeded, monkeypatch, capsys):
    _tape(seeded)
    code, out = run_module(["--cut-tape", "--days", "3", "--confirm", "CUT-TAPE"], seeded, monkeypatch, capsys)
    assert code == 2 and "floor" in out and _count(seeded) == 10


@pytest.mark.parametrize("bad", ["CUT", "CLEAR-HALT", "SHRINK-TAPE"])
def test_another_token_changes_nothing(seeded, monkeypatch, capsys, bad):
    _tape(seeded)
    # The action parses (so the refusal below is the token gate, not argparse
    # rejecting an unknown flag, which also exits 2).
    assert "CUT TAPE DRY RUN" in run_module(["--cut-tape", "--days", "8"], seeded, monkeypatch, capsys)[1]
    code, _ = run_module(["--cut-tape", "--days", "8", "--confirm", bad], seeded, monkeypatch, capsys)
    assert code == 2 and _count(seeded) == 10
