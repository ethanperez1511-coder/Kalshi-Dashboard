from __future__ import annotations

import pytest
from sqlalchemy import Engine

from src.database import get_engine, get_session, Base
from src.models.settings import TradingSettings


@pytest.fixture
def db_engine(tmp_path) -> Engine:
    db_path = tmp_path / "test_settings.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def test_create_default_settings(db_engine: Engine) -> None:
    settings = TradingSettings.get_or_create(db_engine)

    assert settings.id is not None
    assert settings.bankroll == 100.0
    assert settings.mode == "paper"
    assert settings.max_single_trade_pct == 0.03
    assert settings.max_total_exposure_pct == 0.25
    assert settings.max_correlated_exposure_pct == 0.10
    assert settings.daily_loss_limit_pct == 0.05
    assert settings.drawdown_circuit_breaker_pct == 0.20
    assert settings.kelly_fraction == 0.25
    assert settings.paper_trades_before_live == 50
    assert settings.peak_bankroll == 100.0
    assert settings.paper_trade_count == 0


def test_settings_singleton(db_engine: Engine) -> None:
    settings_a = TradingSettings.get_or_create(db_engine)
    settings_b = TradingSettings.get_or_create(db_engine)

    assert settings_a.id == settings_b.id


def test_update_bankroll(db_engine: Engine) -> None:
    # Create initial settings
    TradingSettings.get_or_create(db_engine)

    # Update the bankroll in the database
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        settings.bankroll = 500.0

    # Retrieve again and verify update persisted
    updated = TradingSettings.get_or_create(db_engine)
    assert updated.bankroll == 500.0
