from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import Settings
from src.database import get_engine, Base
from src.api.markets import create_markets_router
from src.api.portfolio import create_portfolio_router
from src.api.backtest import create_backtest_router
from src.api.trading import create_trading_router
from src.demo.seed import seed_demo_data
from src.ingestion.live_ingest import ingest_live_markets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    Base.metadata.create_all(engine)
    app = FastAPI(title="Kalshi Trading Dashboard", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_markets_router(engine))
    app.include_router(create_portfolio_router(engine))
    app.include_router(create_backtest_router(engine))
    app.include_router(create_trading_router(engine))

    @app.on_event("startup")
    async def startup():
        if settings.is_offline_mode:
            logger.info("No Kalshi credentials — running in offline/demo mode")
            seed_demo_data(engine)
        else:
            logger.info("Kalshi credentials found — online mode, ingesting markets")
            ingest_live_markets(engine, settings)

    @app.post("/api/ingest")
    def trigger_ingest():
        count = ingest_live_markets(engine, settings)
        return {"ingested": count}

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
