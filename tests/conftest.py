import pytest
from sqlalchemy import Engine

from src.database import get_engine, Base


@pytest.fixture
def db_engine(tmp_path) -> Engine:
    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def reload_config(monkeypatch):
    """Reload trading_config (and dependents) under a temporary environment.

    Reloading a config module rebinds module-level constants for the REST OF
    THE SESSION, so a test that flips TRADING_MAKER_ENABLED leaves it flipped
    for everything after it — which surfaced as unrelated engine tests failing
    only when run as part of the full suite. The environment is torn down
    explicitly here BEFORE the final reload, because monkeypatch's own undo
    runs after this fixture's finaliser, not before it.
    """
    import importlib
    import os

    touched = []

    def _load(*modules, **env):
        for key, value in env.items():
            touched.append(key)
            os.environ[key] = value
        import src.trading_config as config

        reloaded = importlib.reload(config)
        out = [reloaded]
        for name in modules:
            out.append(importlib.reload(importlib.import_module(name)))
        return out[-1] if modules else reloaded

    yield _load

    for key in touched:
        os.environ.pop(key, None)
    import src.trading_config as config

    importlib.reload(config)
    for name in (
        "src.execution.allowlist",
        "src.ingestion.exclusions",
    ):
        try:
            importlib.reload(importlib.import_module(name))
        except Exception:  # pragma: no cover - module may not be imported
            pass
