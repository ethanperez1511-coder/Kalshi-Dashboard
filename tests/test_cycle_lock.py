"""Two cycles must never run at once. Advisory locks are Postgres-only, so the
behavioural tests need TEST_POSTGRES_URL (see tests/test_shrink_tape.py)."""
from __future__ import annotations

import os

import pytest

from src.cycle_lock import cycle_lock

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="needs TEST_POSTGRES_URL (real Postgres)")


def test_sqlite_always_grants(db_engine):
    with cycle_lock(db_engine) as held:
        assert held


@needs_pg
def test_a_second_cycle_is_refused_while_the_first_holds_it():
    from src.database import get_engine

    first, second = get_engine(PG_URL), get_engine(PG_URL)
    with cycle_lock(first) as held_first:
        assert held_first
        with cycle_lock(second) as held_second:
            assert not held_second


@needs_pg
def test_the_lock_is_free_again_after_the_cycle():
    from src.database import get_engine

    engine = get_engine(PG_URL)
    with cycle_lock(engine) as held:
        assert held
    with cycle_lock(get_engine(PG_URL)) as held_again:
        assert held_again


@needs_pg
def test_a_cycle_that_raises_still_releases_it():
    from src.database import get_engine

    engine = get_engine(PG_URL)
    with pytest.raises(RuntimeError):
        with cycle_lock(engine):
            raise RuntimeError("cycle crashed")
    with cycle_lock(get_engine(PG_URL)) as held:
        assert held


def test_run_pipeline_skips_without_touching_anything_when_locked(monkeypatch):
    """The wiring, not just the lock: run_pipeline returns before the cycle
    body when the lock is refused."""
    import contextlib

    import src.run_trading as rt

    @contextlib.contextmanager
    def refused(engine):
        yield False

    called = []
    monkeypatch.setattr(rt, "cycle_lock", refused)
    monkeypatch.setattr(rt, "require_production_database", lambda url: None)
    monkeypatch.setattr(rt, "get_engine", lambda url: object())
    monkeypatch.setattr(rt, "_run_pipeline_locked", lambda *a: called.append(a))

    assert rt.run_pipeline() is None
    assert called == []
