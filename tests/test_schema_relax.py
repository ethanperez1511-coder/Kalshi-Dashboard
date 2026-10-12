"""A column the model made nullable must become nullable in the database.

orderbook_delta_raw.payload was created NOT NULL on 2026-08-11 and made
nullable in the model on 08-24, for tape compaction. ensure_schema only ever
ADDED columns and WIDENED types, so production kept NOT NULL, and every
compaction since has failed with an IntegrityError (SQLAlchemy code gkpj):
UPDATE ... SET payload = NULL violates the constraint. The failure was caught
as non-fatal, so it was a printed line for seven weeks.

Postgres-only: SQLite does not enforce what this tests.
"""
from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import inspect, text

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="needs TEST_POSTGRES_URL (real Postgres)")
NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def legacy_pg():
    """Production's tape table as it really is: payload NOT NULL."""
    from src.database import Base, get_engine
    import src.models  # noqa: F401

    engine = get_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE orderbook_delta_raw ALTER COLUMN payload SET NOT NULL"))
        for i in range(250):                      # more rows than the batch below
            c.execute(text(
                "INSERT INTO orderbook_delta_raw (market_ticker, msg_type, sid, seq, payload, received_at) "
                "VALUES ('M', 'delta', 1, :i, :p, :t)"
            ), {"i": i, "p": '{"x": 1}', "t": NOW - dt.timedelta(days=5)})
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _payload_nullable(engine) -> bool:
    cols = {c["name"]: c for c in inspect(engine).get_columns("orderbook_delta_raw")}
    return cols["payload"]["nullable"]


def test_the_migrator_reports_and_applies_the_relaxation(legacy_pg):
    from src.database import ensure_schema, pending_schema_changes

    assert "orderbook_delta_raw.payload (relax NOT NULL)" in pending_schema_changes(legacy_pg)
    ensure_schema(legacy_pg)
    assert _payload_nullable(legacy_pg)
    assert pending_schema_changes(legacy_pg) == []


def test_after_migration_compaction_nulls_more_rows_than_one_batch(legacy_pg, monkeypatch):
    import src.maintenance.tape as tape
    from src.database import ensure_schema

    ensure_schema(legacy_pg)
    monkeypatch.setattr(tape, "BATCH", 100)
    result = tape.compact_tape(legacy_pg, tape.plan_compaction(legacy_pg, now=NOW), now=NOW)

    assert result["nulled"] == 250 and not result["stopped"]
    with legacy_pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM orderbook_delta_raw WHERE payload IS NULL")).scalar() == 250


def test_a_primary_key_is_never_relaxed(legacy_pg):
    from src.database import ensure_schema

    ensure_schema(legacy_pg)
    cols = {c["name"]: c for c in inspect(legacy_pg).get_columns("orderbook_delta_raw")}
    assert cols["id"]["nullable"] is False
