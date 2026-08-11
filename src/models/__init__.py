"""Every persisted model, in one place.

Importing this package is what registers the tables on `Base.metadata`, and
migration reads exactly that metadata. A model missing from this list is a
model that will never be created or migrated — `python -m src.migrate` would
report "up to date" and change nothing. `load_all_models()` in database.py
imports this package for that reason.
"""
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.orderbook import OrderbookSnapshot
from src.models.opportunity import Opportunity
from src.models.settings import TradingSettings
from src.models.position import Position
from src.models.trade import Trade
from src.models.odds import OddsCacheEntry, OddsQuotaUsage
from src.models.match_map import MarketMatchMap
from src.backtest.models import BacktestRun, BacktestTrade

__all__ = [
    "Market",
    "PriceSnapshot",
    "OrderbookSnapshot",
    "Opportunity",
    "TradingSettings",
    "Position",
    "Trade",
    "OddsCacheEntry",
    "OddsQuotaUsage",
    "MarketMatchMap",
    "BacktestRun",
    "BacktestTrade",
]
