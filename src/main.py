from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import Settings
from src.database import get_engine, verify_or_migrate
from src.api.markets import create_markets_router
from src.api.portfolio import create_portfolio_router
from src.api.backtest import create_backtest_router
from src.api.trading import create_trading_router
from src.api.quota import create_quota_router
from src.api.matches import create_matches_router
from src.demo.seed import seed_demo_data
from src.ingestion.live_ingest import ingest_live_markets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    # Read-only by default: the dashboard reports a stale schema, it does not
    # fix one. Migrating as a side effect of importing this module would let a
    # `uvicorn src.main:app` (or an import in a test, or a smoke check) reshape
    # whatever database DATABASE_URL happens to point at.
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="the dashboard API")
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
    app.include_router(create_quota_router(engine))
    app.include_router(create_matches_router(engine))

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


def __getattr__(name: str):
    """Build the app on first access to `src.main:app`, not on import.

    uvicorn resolves the target with getattr, so `uvicorn src.main:app` still
    works. But importing this module — from a test, a script, or a tool — no
    longer opens a database connection or checks a schema as a side effect.
    Now that a stale schema raises, an eager module-level `app = create_app()`
    would make the module itself unimportable.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="127.0.0.1", port=8000, reload=True)
