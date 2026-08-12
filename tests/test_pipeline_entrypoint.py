"""Execute run_pipeline end to end.

656 tests passed while `run_pipeline` crashed on line 96 with
`NameError: name 'clock' is not defined`. Every component was tested; the
function that wires them together was not called by anything, so the suite
could not fail on a broken entry point. As a check on the entry point, it was
not one.

An import is not enough either — `import src.run_trading` succeeded throughout,
because a NameError on a local variable only fires when the line executes. The
only thing that catches this class is running the function.

External calls are stubbed and the database is real (SQLite), so what is under
test is the WIRING: name resolution, call signatures, argument order, and the
order of operations.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.settings import TradingSettings


@pytest.fixture
def live_db(db_engine, monkeypatch, tmp_path):
    """A database the pipeline will accept, pointed at by DATABASE_URL."""
    path = tmp_path / "pipeline.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("MIGRATE_ON_BOOT", "true")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    from src.database import ensure_schema, get_engine

    engine = get_engine(url)
    ensure_schema(engine)
    return engine


def _seed_scoreable_market(engine, market_id="KXTEST-26AUG12-T50"):
    now = dt.datetime.now(dt.timezone.utc)
    with get_session(engine) as s:
        s.add(Market(
            market_id=market_id, title="Will something happen?", category="General",
            close_date=now + dt.timedelta(days=2), status="open",
        ))
        s.add(PriceSnapshot(
            market_id=market_id, yes_bid=44, yes_ask=46,
            last_price=45, volume=5000, timestamp=now,
        ))
        s.commit()


@pytest.fixture
def offline_pipeline(monkeypatch):
    """Force the offline branch so no network call is attempted.

    setenv("") rather than delenv(): Settings reads a .env file, so deleting the
    environment variable leaves the file's value in place. The first version of
    this fixture did exactly that, and the test hung fetching 5,000 live markets
    from Kalshi. An empty environment variable shadows the file and is falsy, so
    is_offline_mode becomes True.
    """
    for name in (
        "KALSHI_API_KEY", "KALSHI_API_SECRET",
        "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH",
        "ODDS_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.setenv(name, "")


class TestPipelineExecutes:
    def test_run_pipeline_completes_without_error(self, live_db, offline_pipeline):
        """THE regression: a NameError on line 96 that 656 tests missed."""
        from src.run_trading import run_pipeline

        run_pipeline()          # must not raise

    def test_it_creates_the_settings_row_at_one_hundred(self, live_db, offline_pipeline):
        from src.run_trading import run_pipeline

        run_pipeline()
        with get_session(live_db) as s:
            settings = s.query(TradingSettings).first()
            assert settings is not None
            assert settings.bankroll == 100.0
            assert settings.mode == "paper"

    def test_it_scores_a_seeded_market(self, live_db, offline_pipeline):
        from src.ev.scorer import score_all_markets

        _seed_scoreable_market(live_db)
        results = score_all_markets(live_db)
        assert isinstance(results, list)

    def test_timing_is_recorded_for_every_stage(self, live_db, offline_pipeline):
        """The instrumentation that broke must itself be exercised."""
        from src.run_trading import _Stopwatch

        clock = _Stopwatch()
        clock.mark("ingest")
        clock.mark("settle")
        clock.mark("score")
        assert set(clock.stages) == {"ingest", "settle", "score"}
        assert "total" in clock.summary()

    def test_it_survives_a_second_consecutive_run(self, live_db, offline_pipeline):
        """Idempotence: the cron runs this every five minutes forever."""
        from src.run_trading import run_pipeline

        run_pipeline()
        run_pipeline()

        with get_session(live_db) as s:
            assert s.query(TradingSettings).count() == 1


class TestPipelineRefusals:
    def test_it_refuses_a_zero_bankroll(self, live_db, offline_pipeline):
        from src.bankroll_guard import BankrollNotInitialised
        from src.run_trading import run_pipeline

        with get_session(live_db) as s:
            row = TradingSettings()
            row.bankroll = 0.0
            s.add(row)
            s.commit()

        with pytest.raises(BankrollNotInitialised):
            run_pipeline()

    def test_it_refuses_a_stale_schema(self, monkeypatch, tmp_path, offline_pipeline):
        from sqlalchemy import text

        from src.database import SchemaOutOfDate, ensure_schema, get_engine

        path = tmp_path / "stale.db"
        url = f"sqlite:///{path}"
        engine = get_engine(url)
        ensure_schema(engine)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trades DROP COLUMN deploy_sha"))

        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("MIGRATE_ON_BOOT", "false")

        from src.run_trading import run_pipeline

        with pytest.raises(SchemaOutOfDate):
            run_pipeline()


class TestEveryEntrypointExecutes:
    """Importing a module proves nothing about whether its main() runs.

    Each of these is a scheduled job; a wiring slip in any of them is invisible
    until its cron fires.
    """

    def _env(self, monkeypatch, tmp_path, name):
        from src.database import ensure_schema, get_engine

        path = tmp_path / f"{name}.db"
        url = f"sqlite:///{path}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ensure_schema(get_engine(url))
        return url

    def test_migrate_main_runs(self, monkeypatch, tmp_path):
        self._env(monkeypatch, tmp_path, "migrate")
        from src.migrate import main

        assert main([]) == 0
        assert main(["--check"]) == 0

    def test_maintenance_main_runs(self, monkeypatch, tmp_path):
        self._env(monkeypatch, tmp_path, "maint")
        from src.maintenance.__main__ import main

        assert main([]) == 0            # dry run

    def test_prune_main_runs(self, monkeypatch, tmp_path):
        self._env(monkeypatch, tmp_path, "prune")
        from src.maintenance.prune import main

        assert main(["--dry-run"]) == 0

    def test_day7_main_runs_and_reports_no_data(self, monkeypatch, tmp_path):
        self._env(monkeypatch, tmp_path, "day7")
        from src.execution.day7 import main

        # Exit 1 is correct here: no recorded deltas means nothing to derive.
        assert main([]) == 1

    def test_recorder_main_refuses_without_credentials(
        self, monkeypatch, tmp_path, offline_pipeline,
    ):
        self._env(monkeypatch, tmp_path, "rec")
        from src.recorder.__main__ import main

        assert main(["--duration", "1"]) == 1
