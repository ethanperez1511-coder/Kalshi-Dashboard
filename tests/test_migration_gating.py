"""Migrations run only when asked (Phase 1.5, 2026-08-11).

`ensure_schema` was called from `create_app()`, so *importing* the dashboard
migrated whatever database `DATABASE_URL` pointed at. A smoke test of the API
during Phase 1 silently altered the real `kalshi.db` — additive and safe that
time, but a read-only tool must not reshape production as a side effect, and
the migration may be for code the booting process is not even running.

Now: booting verifies and refuses; `python -m src.migrate` is the only path
that writes by default.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

import pytest

from src.database import (
    Base,
    SchemaOutOfDate,
    ensure_schema,
    pending_schema_changes,
    verify_or_migrate,
)
from src.migrate import main as migrate_main


def _legacy_trades(engine):
    """The trades table as it was before entry_fee — i.e. a stale schema."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS trades"))
        conn.execute(text(
            "CREATE TABLE trades ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, market_id VARCHAR(100),"
            " side VARCHAR(10), action VARCHAR(10), price INTEGER,"
            " quantity INTEGER, p_model FLOAT, implied_prob FLOAT, edge FLOAT,"
            " net_ev FLOAT, position_size_dollars FLOAT, confidence FLOAT,"
            " reasoning TEXT, is_paper BOOLEAN, status VARCHAR(20),"
            " order_id VARCHAR(100), exit_price INTEGER, realized_pnl FLOAT,"
            " created_at DATETIME)"
        ))


class TestDetection:
    def test_pending_lists_missing_columns(self, db_engine):
        Base.metadata.create_all(db_engine)
        _legacy_trades(db_engine)
        pending = pending_schema_changes(db_engine)
        assert "trades.entry_fee" in pending
        assert "trades.entry_fee_source" in pending

    def test_pending_is_empty_when_current(self, db_engine):
        Base.metadata.create_all(db_engine)
        assert pending_schema_changes(db_engine) == []

    def test_pending_writes_nothing(self, db_engine):
        Base.metadata.create_all(db_engine)
        _legacy_trades(db_engine)
        pending_schema_changes(db_engine)
        # Still stale: detection must not have "helpfully" fixed it.
        assert "trades.entry_fee" in pending_schema_changes(db_engine)


class TestMetadataCompleteness:
    """`python -m src.migrate` imports Settings and database — and nothing else.

    Table metadata is populated as a side effect of importing model modules, so
    without an explicit load the CLI would inspect an empty metadata, report
    "up to date", migrate nothing, and leave the pipeline to crash on the very
    column it was run to add. This asserts every model is reachable.
    """

    def test_every_model_table_is_registered(self):
        from src.database import Base, load_all_models

        load_all_models()
        names = set(Base.metadata.tables)
        for expected in (
            "markets", "price_snapshots", "orderbook_snapshots", "opportunities",
            "trading_settings", "positions", "trades", "odds_cache", "odds_quota",
            "market_match_map", "backtest_runs", "backtest_trades",
        ):
            assert expected in names, f"{expected} would never be migrated"

    def test_migrate_creates_a_virgin_database_completely(self, monkeypatch, tmp_path):
        """Fresh Neon-style DB: the CLI alone must build the whole schema."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/virgin.db")
        assert migrate_main([]) == 0

        from src.database import get_engine
        engine = get_engine(f"sqlite:///{tmp_path}/virgin.db")
        assert pending_schema_changes(engine) == []
        insp = inspect(engine)
        assert "market_match_map" in insp.get_table_names()
        assert "odds_cache" in insp.get_table_names()


class TestGate:
    def test_refuses_and_changes_nothing_when_flag_is_off(self, db_engine, caplog):
        Base.metadata.create_all(db_engine)
        _legacy_trades(db_engine)
        with pytest.raises(SchemaOutOfDate):
            verify_or_migrate(db_engine, migrate=False, context="test")
        assert "trades.entry_fee" in pending_schema_changes(db_engine)
        assert "SCHEMA IS BEHIND" in caplog.text
        assert "python -m src.migrate" in caplog.text

    def test_applies_when_flag_is_on(self, db_engine):
        Base.metadata.create_all(db_engine)
        _legacy_trades(db_engine)
        added = verify_or_migrate(db_engine, migrate=True, context="test")
        assert "trades.entry_fee" in added
        assert pending_schema_changes(db_engine) == []

    def test_is_a_no_op_on_a_current_schema(self, db_engine):
        Base.metadata.create_all(db_engine)
        assert verify_or_migrate(db_engine, migrate=False, context="test") == []


class TestCli:
    def _point_at(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")

    def test_check_reports_without_writing(self, monkeypatch, tmp_path):
        self._point_at(monkeypatch, tmp_path)
        from src.database import get_engine
        engine = get_engine(f"sqlite:///{tmp_path}/cli.db")
        Base.metadata.create_all(engine)
        _legacy_trades(engine)

        assert migrate_main(["--check"]) == 1
        assert "trades.entry_fee" in pending_schema_changes(engine)

    def test_apply_migrates_and_is_idempotent(self, monkeypatch, tmp_path):
        self._point_at(monkeypatch, tmp_path)
        from src.database import get_engine
        engine = get_engine(f"sqlite:///{tmp_path}/cli.db")
        Base.metadata.create_all(engine)
        _legacy_trades(engine)

        assert migrate_main([]) == 0
        assert pending_schema_changes(engine) == []
        assert migrate_main([]) == 0        # second run: nothing to do
        assert migrate_main(["--check"]) == 0


class TestAppBootDoesNotMigrate:
    def test_create_app_raises_instead_of_migrating(self, monkeypatch, tmp_path):
        """The exact Phase 1 incident: booting the API must not touch schema."""
        db = tmp_path / "boot.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
        monkeypatch.setenv("MIGRATE_ON_BOOT", "false")

        from src.database import get_engine
        engine = get_engine(f"sqlite:///{db}")
        ensure_schema(engine)   # a current database...
        _legacy_trades(engine)  # ...that then falls behind the models

        from src.main import create_app
        with pytest.raises(SchemaOutOfDate):
            create_app()

        # Unchanged: the boot attempt must not have migrated anything.
        assert "trades.entry_fee" in pending_schema_changes(engine)

    def test_create_app_migrates_when_explicitly_allowed(self, monkeypatch, tmp_path):
        db = tmp_path / "boot_ok.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
        monkeypatch.setenv("MIGRATE_ON_BOOT", "true")

        from src.database import get_engine
        engine = get_engine(f"sqlite:///{db}")
        ensure_schema(engine)
        _legacy_trades(engine)

        from src.main import create_app
        create_app()
        assert pending_schema_changes(engine) == []
