"""Divergences where SQLite is lenient and Postgres is not.

The first Postgres-only failure: markets.title was VARCHAR(500) and a
multi-leg parlay title measured 1,381 characters. SQLite ignores VARCHAR
lengths entirely, so months of local runs could not catch it. These tests pin
the whole class rather than the one instance.
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Boolean, Integer, String

from src.database import Base, get_session, load_all_models
from src.models.market import Market


class TestUnboundedApiStringsAreText:
    """Columns fed by unbounded API text must not carry a length limit."""

    @pytest.mark.parametrize("table,column", [
        ("markets", "title"),          # measured 1381 chars
        ("markets", "market_id"),
        ("markets", "category"),
        ("trades", "market_id"),
        ("trades", "order_id"),
        ("positions", "market_id"),
        ("price_snapshots", "market_id"),
        ("opportunities", "market_id"),
        ("opportunities", "model_name"),
        ("orderbook_delta_raw", "market_ticker"),
        ("market_match_map", "kalshi_market_id"),
        ("market_match_map", "poly_condition_id"),
        ("shadow_maker_orders", "market_id"),
    ])
    def test_column_has_no_length_limit(self, table, column):
        load_all_models()
        col = Base.metadata.tables[table].c[column]
        length = getattr(col.type, "length", None)
        assert length is None, (
            f"{table}.{column} is VARCHAR({length}); it is fed by API text with "
            f"no guaranteed bound and will truncate on Postgres"
        )

    def test_a_1381_character_title_round_trips(self, db_engine):
        """The exact production failure, as a regression."""
        import datetime as dt

        Base.metadata.create_all(db_engine)
        title = "yes Shane Baz: 3+,yes Zebby Matthews: 3+," * 34
        assert len(title) > 1381
        with get_session(db_engine) as s:
            s.add(Market(
                market_id="KXMVESPORTS-LONG", title=title, category="Sports",
                close_date=dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc),
                status="open",
            ))
            s.commit()
        with get_session(db_engine) as s:
            assert len(s.query(Market).filter_by(market_id="KXMVESPORTS-LONG").one().title) == len(title)


class TestEnumeratedColumnsKeepTheirBound:
    """A length limit is correct where the bound is semantic — it documents the
    domain and catches a bad write. Widening everything would lose that."""

    @pytest.mark.parametrize("table,column", [
        ("trades", "side"), ("trades", "status"),
        ("positions", "side"), ("trading_settings", "mode"),
        ("markets", "terms_status"), ("markets", "strike_direction"),
    ])
    def test_enumerated_column_is_still_bounded(self, table, column):
        load_all_models()
        col = Base.metadata.tables[table].c[column]
        assert getattr(col.type, "length", None) is not None


class TestIntegerOverflow:
    def test_epoch_ms_columns_are_bigint(self):
        """An epoch-ms value is ~1.79e12 and overflows Postgres int4 (2.1e9).
        SQLite has no such limit, so this could only fail in production."""
        load_all_models()
        for table, column in [
            ("orderbook_delta_raw", "ts_ms"),
            ("orderbook_delta_raw", "seq"),
            ("shadow_maker_orders", "rest_start_ms"),
        ]:
            col = Base.metadata.tables[table].c[column]
            assert isinstance(col.type, BigInteger), f"{table}.{column} must be BigInteger"

    def test_a_real_epoch_ms_value_exceeds_int4(self):
        assert 1786471379310 > 2_147_483_647


class TestBooleanDefaults:
    def test_boolean_server_defaults_are_boolean_literals(self):
        """Postgres rejects DEFAULT 0 on a boolean column; it wants a boolean
        literal. SQLite accepts either."""
        load_all_models()
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, Boolean) and col.server_default is not None:
                    value = str(col.server_default.arg).strip().lower()
                    assert value in ("true", "false"), (
                        f"{table.name}.{col.name} server_default={value!r} — "
                        f"Postgres needs a boolean literal"
                    )


class TestWideningMigration:
    def test_widening_is_detected(self):
        from sqlalchemy import Column, Text
        from src.database import _widening

        clause = _widening(None, "markets", Column("title", Text()),
                           {"name": "title", "type": "VARCHAR(500)"})
        assert clause == "ALTER COLUMN title TYPE TEXT"

    def test_narrowing_is_never_generated(self):
        """Widening only. The reverse would truncate data."""
        from sqlalchemy import Column, String
        from src.database import _widening

        assert _widening(None, "markets", Column("title", String(500)),
                         {"name": "title", "type": "TEXT"}) is None

    def test_integer_to_bigint_is_detected(self):
        from sqlalchemy import BigInteger as BI, Column
        from src.database import _widening

        assert _widening(None, "t", Column("ts_ms", BI()),
                         {"name": "ts_ms", "type": "INTEGER"}) == \
            "ALTER COLUMN ts_ms TYPE BIGINT"

    def test_matching_types_need_no_change(self):
        from sqlalchemy import Column, Text
        from src.database import _widening

        assert _widening(None, "t", Column("x", Text()),
                         {"name": "x", "type": "TEXT"}) is None


class TestBankrollGuard:
    def _settings(self, engine, bankroll):
        from src.models.settings import TradingSettings

        Base.metadata.create_all(engine)
        with get_session(engine) as s:
            row = TradingSettings()
            row.bankroll = bankroll
            s.add(row)
            s.commit()

    def test_no_settings_row_is_not_an_error(self, db_engine):
        """$0.00 before the first cycle means NO ROW, not a drained account."""
        from src.bankroll_guard import check_bankroll

        Base.metadata.create_all(db_engine)
        ok, message = check_bankroll(db_engine)
        assert ok is True
        assert "no settings row" in message

    def test_zero_bankroll_is_refused(self, db_engine):
        """Kelly sizes everything to zero: approves trades, places nothing,
        reports success. A silent do-nothing bot."""
        from src.bankroll_guard import BankrollNotInitialised, assert_bankroll_workable

        self._settings(db_engine, 0.0)
        with pytest.raises(BankrollNotInitialised) as exc:
            assert_bankroll_workable(db_engine)
        assert "sizes everything to zero" in str(exc.value)

    def test_a_real_bankroll_passes(self, db_engine):
        """The guard must not be a blanket refusal."""
        from src.bankroll_guard import assert_bankroll_workable

        self._settings(db_engine, 100.0)
        assert_bankroll_workable(db_engine)

    def test_get_or_create_initialises_at_one_hundred(self, db_engine):
        """Where the $100 comes from: the column default, applied at INSERT by
        get_or_create on the first cycle — not at object construction, which is
        why an unflushed TradingSettings() reads None."""
        from src.models.settings import TradingSettings

        Base.metadata.create_all(db_engine)
        settings = TradingSettings.get_or_create(db_engine)
        assert settings.bankroll == 100.0

    def test_first_cycle_creates_a_workable_bankroll(self, db_engine):
        """End to end: no row -> get_or_create -> guard passes."""
        from src.bankroll_guard import assert_bankroll_workable
        from src.models.settings import TradingSettings

        Base.metadata.create_all(db_engine)
        TradingSettings.get_or_create(db_engine)
        assert_bankroll_workable(db_engine)
