"""The Polymarket review queue, readable from Actions.

Pending pairs live only in Neon, and the dashboard's /matches/pending reads a
local database. Verdicts reach production through match_seed.py, so the
operator needs the queue printed where Neon is reachable. Read-only, no token.
"""
from __future__ import annotations

import datetime as dt

from src.database import Base, get_session
from src.models.match_map import MarketMatchMap
from tests.test_maintenance_entrypoint import run_module


def test_it_lists_every_pending_pair_and_nothing_else(db_engine, monkeypatch, capsys):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as s:
        s.add(MarketMatchMap(kalshi_market_id="KXA-1", poly_condition_id="0xaaa",
                             status="pending", similarity=0.91,
                             kalshi_title="Will A happen?", poly_question="A happens?",
                             created_at=dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)))
        from src.models.market import Market

        s.add(Market(market_id="KXA-1", title="Will A happen?", category="Politics",
                     close_date=dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc),
                     status="active", rules="Resolves YES if A happens by Dec 31."))
        s.add(MarketMatchMap(kalshi_market_id="KXB-1", poly_condition_id="0xbbb",
                             status="blocked", similarity=0.88,
                             kalshi_title="Will B?", poly_question="B?"))
        s.commit()

    code, out = run_module(["--pending-matches"], db_engine, monkeypatch, capsys)

    assert code == 0
    assert "KXA-1" in out and "0xaaa" in out and "A happens?" in out
    assert "KXB-1" not in out
    assert "Resolves YES if A happens by Dec 31." in out    # the rules, for L8
    with get_session(db_engine) as s:
        assert s.query(MarketMatchMap).count() == 2         # read-only


def test_an_empty_queue_says_so(db_engine, monkeypatch, capsys):
    import src.models  # noqa: F401

    Base.metadata.create_all(db_engine)
    code, out = run_module(["--pending-matches"], db_engine, monkeypatch, capsys)
    assert code == 0 and "0 pending" in out
