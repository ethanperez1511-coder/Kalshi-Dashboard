from __future__ import annotations

import logging

from fastapi import FastAPI

from src.config import Settings
from src.database import get_engine, Base
from src.api.markets import create_markets_router
from src.demo.seed import seed_demo_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    Base.metadata.create_all(engine)
    app = FastAPI(title="Kalshi Trading Dashboard", version="0.1.0")
    app.include_router(create_markets_router(engine))

    @app.on_event("startup")
    async def startup():
        if settings.is_offline_mode:
            logger.info("No Kalshi credentials — running in offline/demo mode")
            seed_demo_data(engine)
        else:
            logger.info("Kalshi credentials found — online mode")

    @app.get("/api/status")
    def get_status():
        return {
            "mode": "offline" if settings.is_offline_mode else "online",
            "version": "0.1.0",
        }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="127.0.0.1", port=8000, reload=True)
