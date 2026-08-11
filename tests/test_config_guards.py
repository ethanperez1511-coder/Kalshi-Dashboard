"""DATABASE_URL configuration guards.

All four workflows failed identically on `Could not parse SQLAlchemy URL from
given URL string`. The cause was not a missing env block — every step had one —
but an unset repository secret: GitHub still sets the variable, to the empty
string, and an empty env var OVERRIDES a default rather than falling back to it.
The traceback named neither the variable nor the cause.

Per the pinned principle, each guard is shown failing on the exact input that
broke production.
"""
from __future__ import annotations

import pytest

from src.config import (
    ConfigError,
    Settings,
    normalise_database_url,
    require_production_database,
)


class TestEmptyUrlNamesItself:
    @pytest.mark.parametrize("value", ["", "   ", "\t", None])
    def test_empty_raises_a_message_that_names_the_variable(self, value):
        with pytest.raises(ConfigError) as exc:
            normalise_database_url(value)
        message = str(exc.value)
        assert "DATABASE_URL is not set" in message
        assert "secret" in message

    def test_settings_raises_on_an_empty_env_var(self, monkeypatch):
        """THE production failure: secret unset -> env var empty -> parse error."""
        monkeypatch.setenv("DATABASE_URL", "")
        with pytest.raises(Exception) as exc:
            Settings()
        assert "DATABASE_URL is not set" in str(exc.value)


class TestSchemeNormalisation:
    def test_neon_style_url_gets_the_psycopg3_driver(self):
        """Neon hands out postgresql://, whose default driver is psycopg2 —
        this project installs psycopg3. The next failure in line."""
        assert normalise_database_url("postgresql://u:p@h/db") == \
            "postgresql+psycopg://u:p@h/db"

    def test_legacy_postgres_scheme_is_also_normalised(self):
        assert normalise_database_url("postgres://u:p@h/db") == \
            "postgresql+psycopg://u:p@h/db"

    def test_an_explicit_driver_is_left_alone(self):
        url = "postgresql+psycopg://u:p@h/db"
        assert normalise_database_url(url) == url

    def test_sqlite_is_untouched(self):
        assert normalise_database_url("sqlite:///kalshi.db") == "sqlite:///kalshi.db"

    def test_whitespace_is_stripped(self):
        assert normalise_database_url("  sqlite:///x.db\n") == "sqlite:///x.db"


class TestEphemeralDatabaseRefusal:
    def test_sqlite_in_actions_is_refused(self, monkeypatch):
        """Falling back to the local default inside a runner is worse than
        crashing: green job, database written to a filesystem that is deleted
        when the job ends, every cycle reporting success and persisting nothing."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with pytest.raises(ConfigError) as exc:
            require_production_database("sqlite:///kalshi.db")
        assert "ephemeral" in str(exc.value)
        assert "persist nothing" in str(exc.value)

    def test_postgres_in_actions_is_allowed(self, monkeypatch):
        """The guard must not be a blanket refusal, or nobody would notice it
        was broken."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        require_production_database("postgresql+psycopg://u:p@h/db")   # no raise

    def test_sqlite_locally_is_allowed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        require_production_database("sqlite:///kalshi.db")             # no raise


class TestEveryEntrypointIsGuarded:
    """A guard applied one entrypoint at a time recurs one entrypoint at a time."""

    @pytest.mark.parametrize("module", [
        "src.migrate",
        "src.run_trading",
        "src.weather.refit_job",
        "src.recorder.__main__",
        "src.maintenance.__main__",
    ])
    def test_entrypoint_calls_the_production_guard(self, module):
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(module))
        assert "require_production_database" in source, (
            f"{module} builds an engine without checking it is a real database"
        )
