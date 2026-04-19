from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    KALSHI_API_KEY: str = ""
    KALSHI_API_SECRET: str = ""
    DATABASE_URL: str = "sqlite:///kalshi.db"
    POLL_INTERVAL_SECONDS: int = 60
    WS_PRICE_INTERVAL_SECONDS: int = 10
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_WS_URL: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    @property
    def is_offline_mode(self) -> bool:
        return not self.KALSHI_API_KEY or not self.KALSHI_API_SECRET

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
