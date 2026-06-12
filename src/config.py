from pydantic_settings import BaseSettings


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

    @property
    def is_offline_mode(self) -> bool:
        has_key = bool(self.KALSHI_API_KEY)
        has_auth = bool(self.KALSHI_API_SECRET) or bool(self.KALSHI_PRIVATE_KEY_PATH) or bool(self.KALSHI_PRIVATE_KEY)
        return not (has_key and has_auth)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
