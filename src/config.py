import os

from pydantic import field_validator
from pydantic_settings import BaseSettings


class ConfigError(RuntimeError):
    """A configuration value is present but unusable. Names itself."""


def normalise_database_url(raw: str) -> str:
    """Validate and normalise a database URL, or raise something readable.

    An unset secret in GitHub Actions still sets the env var — to the empty
    string — and an empty env var OVERRIDES a default rather than falling back
    to it. So `${{ secrets.DATABASE_URL }}` with no secret configured produced
    `get_engine("")` and a bare SQLAlchemy parse traceback that named neither
    the variable nor the cause.

    Also fixes the next failure in line: Neon hands out `postgresql://`, whose
    default driver is psycopg2, but this project installs psycopg3. Left alone
    that fails at connect time with ModuleNotFoundError instead of anything
    resembling a configuration message.
    """
    if raw is None or not str(raw).strip():
        raise ConfigError(
            "DATABASE_URL is not set (or is empty) — check the workflow env "
            "block and that the DATABASE_URL repository secret exists. An "
            "unset GitHub secret still sets the variable to an empty string, "
            "which overrides the default rather than falling back to it."
        )

    url = str(raw).strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def require_production_database(url: str) -> None:
    """Refuse to run a production job against ephemeral SQLite.

    Falling back to the local default inside a runner is worse than crashing:
    the job goes green, writes a database into a container filesystem, and
    that filesystem is deleted when the job ends. Every cycle would report
    success and persist nothing.
    """
    if os.environ.get("GITHUB_ACTIONS") and url.startswith("sqlite"):
        raise ConfigError(
            f"Refusing to run in Actions against {url!r}. This is ephemeral "
            f"container storage — the job would go green and persist nothing. "
            f"Set the DATABASE_URL repository secret to the Neon connection "
            f"string."
        )


class Settings(BaseSettings):
    TELEGRAM_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    KALSHI_API_KEY: str = ""
    KALSHI_API_SECRET: str = ""
    KALSHI_PRIVATE_KEY_PATH: str = ""
    KALSHI_PRIVATE_KEY: str = ""  # PEM content directly (for cloud deploy)
    ODDS_API_KEY: str = ""  # From the-odds-api.com (free tier)
    DATABASE_URL: str = "sqlite:///kalshi.db"
    POLL_INTERVAL_SECONDS: int = 60
    WS_PRICE_INTERVAL_SECONDS: int = 10
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_WS_URL: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    # A schema migration is a deliberate act. Starting a process must never
    # alter the database it was pointed at — run `python -m src.migrate`.
    # Left False, a stale schema is logged as an error and the process refuses
    # to run rather than silently migrating.
    MIGRATE_ON_BOOT: bool = False

    @property
    def is_offline_mode(self) -> bool:
        has_key = bool(self.KALSHI_API_KEY)
        has_auth = bool(self.KALSHI_API_SECRET) or bool(self.KALSHI_PRIVATE_KEY_PATH) or bool(self.KALSHI_PRIVATE_KEY)
        return not (has_key and has_auth)

    @field_validator("DATABASE_URL")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        return normalise_database_url(value)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
