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


# --------------------------------------------------------------------------
# Round-trip counts: the 8-minute timeout class
# --------------------------------------------------------------------------

class TestBatchedWrites:
    """Row-at-a-time ORM work is a function call on SQLite and a network
    round-trip on Neon. The first production cycle spent its whole 8-minute
    budget on ~135k of them before scoring began."""

    def _markets(self, n, prefix="M"):
        import datetime as dtm
        from src.kalshi.schemas import KalshiMarket

        return [
            KalshiMarket(
                ticker=f"{prefix}-{i}", title=f"t{i}", category="Sports",
                close_time=dtm.datetime(2026, 12, 31, tzinfo=dtm.timezone.utc),
                status="open", yes_bid=44, yes_ask=46, last_price=45, volume=100,
            )
            for i in range(n)
        ]

    def test_sync_is_a_bounded_number_of_statements(self, db_engine):
        """Not proportional to market count."""
        from sqlalchemy import event
        from src.ingestion.market_sync import sync_markets

        Base.metadata.create_all(db_engine)
        counter = {"n": 0}

        @event.listens_for(db_engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        sync_markets(db_engine, self._markets(500))
        # One SELECT + one bulk INSERT + transaction control. Nowhere near 500.
        assert counter["n"] < 25, f"{counter['n']} statements for 500 markets"

    def test_duplicate_tickers_in_one_batch_do_not_break_the_insert(self, db_engine):
        from src.ingestion.market_sync import sync_markets

        Base.metadata.create_all(db_engine)
        markets = self._markets(1) * 3
        sync_markets(db_engine, markets)
        with get_session(db_engine) as s:
            assert s.query(Market).count() == 1

    def test_sync_updates_existing_rows(self, db_engine):
        from src.ingestion.market_sync import sync_markets

        Base.metadata.create_all(db_engine)
        sync_markets(db_engine, self._markets(3))
        changed = self._markets(3)
        changed[0].title = "updated title"
        sync_markets(db_engine, changed)

        with get_session(db_engine) as s:
            assert s.query(Market).count() == 3
            assert s.query(Market).filter_by(market_id="M-0").one().title == "updated title"

    def test_snapshot_batch_is_a_bounded_number_of_statements(self, db_engine):
        from sqlalchemy import event
        from src.ingestion.price_recorder import record_price_snapshots

        Base.metadata.create_all(db_engine)
        counter = {"n": 0}

        @event.listens_for(db_engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        quotes = [(f"M-{i}", 44, 46, 45, 100) for i in range(500)]
        written = record_price_snapshots(db_engine, quotes)
        assert written == 500
        assert counter["n"] < 25, f"{counter['n']} statements for 500 snapshots"

    def test_batch_writer_applies_the_same_suppression_rules(self, db_engine):
        import datetime as dtm
        from src.ingestion.price_recorder import record_price_snapshots

        Base.metadata.create_all(db_engine)
        now = dtm.datetime(2026, 8, 12, 12, tzinfo=dtm.timezone.utc)

        assert record_price_snapshots(db_engine, [("DEAD", 0, 0, 0, 0)], now=now) == 0
        assert record_price_snapshots(db_engine, [("M", 44, 46, 45, 100)], now=now) == 1
        # Identical quote five minutes later is suppressed.
        assert record_price_snapshots(
            db_engine, [("M", 44, 46, 45, 100)], now=now + dtm.timedelta(minutes=5),
        ) == 0
        # A changed quote always writes.
        assert record_price_snapshots(
            db_engine, [("M", 45, 47, 46, 120)], now=now + dtm.timedelta(minutes=6),
        ) == 1

    def test_scorer_does_not_query_per_market(self, db_engine):
        """The specific regression: one session per market for its snapshot."""
        import datetime as dtm
        from sqlalchemy import event
        from src.ev.scorer import score_all_markets
        from src.ingestion.market_sync import sync_markets
        from src.ingestion.price_recorder import record_price_snapshots

        Base.metadata.create_all(db_engine)
        sync_markets(db_engine, self._markets(300))
        record_price_snapshots(
            db_engine, [(f"M-{i}", 44, 46, 45, 100) for i in range(300)],
        )

        counter = {"n": 0}

        @event.listens_for(db_engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        score_all_markets(db_engine)
        # Was ~3 per market (snapshot fetch + opportunity SELECT/INSERT/COMMIT),
        # plus per-market queries inside price-derived models whose output the
        # gate discarded anyway. Snapshot load and opportunity upsert are now
        # batched, and gated-off models no longer run at all.
        assert counter["n"] < 60, f"{counter['n']} statements for 300 markets"


# --------------------------------------------------------------------------
# Round-trip counts, part 2: inside the models
# --------------------------------------------------------------------------

class TestBatchedModelQueries:
    """The scorer's own queries were batched first, and the stage still took
    191 seconds and scored one market. The rest was inside the models: a
    decision lookup per market, a fit/guard/terms/ladder lookup per market, and
    — invisible to any statement counter — a full re-tokenisation of every
    Polymarket candidate per market.
    """

    @staticmethod
    def _counter(engine):
        from sqlalchemy import event

        box = {"n": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            box["n"] += 1

        return box

    # -- Polymarket ------------------------------------------------------

    def test_polymarket_does_not_query_decisions_per_market(self, db_engine):
        from src.modeling.models.polymarket import PolymarketModel
        from src.modeling.polymarket_api import PolyMarket

        class _Client:
            def get_markets(self):
                return [PolyMarket(question=f"Will thing {i} happen in 2026?",
                                   yes_price=0.4, volume_usd=500_000.0)
                        for i in range(50)]

        model = PolymarketModel(poly_client=_Client())
        box = self._counter(db_engine)
        for i in range(100):
            model.estimate(f"K-{i}", f"Will other thing {i} occur by 2027?", 0.5, db_engine)
        # One all_decisions() load, and nothing after it. Was 1 per market.
        assert box["n"] < 10, f"{box['n']} statements for 100 markets"

    def test_polymarket_tokenises_each_candidate_once(self, db_engine, monkeypatch):
        """The cost a statement counter cannot see.

        `_match_market` ran `_normalize_tokens` over the whole candidate set for
        every market scored. At the production scan limit that is ~2,000 regex
        passes per market — pure CPU, zero queries, and the single largest
        component of the 191-second stage.
        """
        from src.modeling.models import polymarket as pm
        from src.modeling.polymarket_api import PolyMarket

        candidates = [PolyMarket(question=f"Will thing {i} happen in 2026?",
                                 yes_price=0.4, volume_usd=500_000.0)
                      for i in range(200)]

        class _Client:
            def get_markets(self):
                return candidates

        calls = {"n": 0}
        real = pm._normalize_tokens

        def _spy(title):
            calls["n"] += 1
            return real(title)

        monkeypatch.setattr(pm, "_normalize_tokens", _spy)

        model = pm.PolymarketModel(poly_client=_Client())
        markets = 40
        for i in range(markets):
            model.estimate(f"K-{i}", f"Will other thing {i} occur by 2027?", 0.5, db_engine)

        # 200 candidates tokenised once, plus one pass per market title.
        # The old code cost 200 * 40 = 8,000.
        assert calls["n"] <= len(candidates) + 2 * markets, (
            f"{calls['n']} tokenisations for {markets} markets against "
            f"{len(candidates)} candidates — candidate set is being re-tokenised"
        )

    def test_polymarket_index_is_per_cycle_not_per_process(self):
        """Cycle scope comes from the model being rebuilt each run. If the cache
        were module-level, a market listed mid-session would never be seen."""
        from src.modeling.models.polymarket import PolymarketModel
        from src.modeling.polymarket_api import PolyMarket

        class _Client:
            def get_markets(self):
                return [PolyMarket(question="Will thing happen in 2026?",
                                   yes_price=0.4, volume_usd=500_000.0)]

        assert PolymarketModel(poly_client=_Client())._index is None
        assert PolymarketModel(poly_client=_Client())._decisions is None

    # -- Weather ---------------------------------------------------------

    def test_weather_does_not_query_per_contract_in_a_ladder(self, db_engine):
        """A city-day ladder is six contracts sharing one station, one fit, one
        guard and one MOS forecast. It was six of each."""
        import datetime as dtm

        from src.database import get_session
        from src.models.market import Market as M, TERMS_PARSED
        from src.models.price import PriceSnapshot
        from src.weather.calibration import CellFit
        from src.weather.fitting import save_fit
        from src.weather.model import WeatherModel

        from tests.test_weather_model import _RULES_TEXT as _SETTLEMENT_RULES, TODAY, _Mos

        Base.metadata.create_all(db_engine)
        tickers = [f"KXHIGHNY-26AUG12-T{t}" for t in (80, 85, 90, 95, 100, 105)]
        with get_session(db_engine) as s:
            for i, ticker in enumerate(tickers):
                s.add(M(
                    market_id=ticker, title="Will the high temp in NYC be >90°?",
                    category="Climate and Weather",
                    close_date=dtm.datetime(2026, 8, 13, 4, 59, tzinfo=dtm.timezone.utc),
                    status="open", series_ticker="KXHIGHNY",
                    strike_direction="above", strike_value=80.0 + 5 * i,
                    strike_unit="F", terms_status=TERMS_PARSED,
                    # The settlement guard reads this; a market row without it
                    # cannot show where it settles and is refused. Production
                    # always stores rules_primary.
                    rules=_SETTLEMENT_RULES,
                ))
                s.add(PriceSnapshot(market_id=ticker, yes_bid=40, yes_ask=44,
                                    last_price=42, volume=500))
            s.commit()
        save_fit(
            db_engine,
            CellFit("KNYC", 1, 0.4, 3.2, 160, dtm.date(2025, 4, 1), dtm.date(2025, 8, 31)),
            True, [], 0.62, 1.03, 105,
        )

        http = _Mos(temp=93.0)
        fetches = {"n": 0}
        real_get = http.get

        def _counted_get(*a, **kw):
            fetches["n"] += 1
            return real_get(*a, **kw)

        http.get = _counted_get
        model = WeatherModel(http=http, now=lambda: TODAY)

        box = self._counter(db_engine)
        results = [model.estimate(t, "t", 0.42, db_engine) for t in tickers]

        assert all(r is not None for r in results), "ladder should price"
        # terms + fit + guard + ladder, each loaded once for the whole ladder.
        assert box["n"] < 15, f"{box['n']} statements for {len(tickers)} contracts"
        assert fetches["n"] == 1, (
            f"{fetches['n']} MOS fetches for one station-day — the forecast is "
            "identical for every contract on the ladder"
        )
