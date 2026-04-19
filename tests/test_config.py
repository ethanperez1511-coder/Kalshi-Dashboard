from src.config import Settings


def test_default_settings():
    settings = Settings(
        KALSHI_API_KEY="",
        KALSHI_API_SECRET="",
    )
    assert settings.DATABASE_URL == "sqlite:///kalshi.db"
    assert settings.KALSHI_API_KEY == ""
    assert settings.KALSHI_API_SECRET == ""
    assert settings.POLL_INTERVAL_SECONDS == 60
    assert settings.WS_PRICE_INTERVAL_SECONDS == 10


def test_offline_mode_when_no_credentials():
    settings = Settings(
        KALSHI_API_KEY="",
        KALSHI_API_SECRET="",
    )
    assert settings.is_offline_mode is True


def test_online_mode_when_credentials_set():
    settings = Settings(
        KALSHI_API_KEY="test_key",
        KALSHI_API_SECRET="test_secret",
    )
    assert settings.is_offline_mode is False
