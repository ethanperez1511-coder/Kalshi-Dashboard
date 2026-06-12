"""Central config flags for the trading system.

All flags have sensible defaults and can be overridden via environment
variables (prefixed TRADING_) or by importing and mutating this module's
attributes before the pipeline runs.
"""
from __future__ import annotations

import os


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "")
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "")
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


# --- Change 2: Parlay leg correlation ---
SKIP_SAME_GAME_PARLAYS: bool = _env_bool("TRADING_SKIP_SAME_GAME_PARLAYS", True)

# --- Change 3: Model type separation ---
TRADE_PRICE_DERIVED_MODELS: bool = _env_bool("TRADING_TRADE_PRICE_DERIVED_MODELS", False)
PRICE_DERIVED_MIN_EDGE: float = _env_float("TRADING_PRICE_DERIVED_MIN_EDGE", 0.10)
ENABLE_KEYWORD_PRIORS: bool = _env_bool("TRADING_ENABLE_KEYWORD_PRIORS", False)

# --- Change 4: Spread threshold ---
MAX_SPREAD_CENTS: int = _env_int("TRADING_MAX_SPREAD_CENTS", 3)

# --- Pre-live hardening: disagreement cap ---
# Reject trades where |p_model - market| exceeds this. Settled-trade data showed
# huge claimed edges (avg +29% on YES) losing money — big disagreement means bad
# data, not alpha.
MAX_MODEL_DISAGREEMENT: float = _env_float("TRADING_MAX_MODEL_DISAGREEMENT", 0.15)

# --- Pre-live hardening: YES longshot skip ---
# YES buys under this price bled -$2.86/trade in paper; fading longshots (NO side) won.
SKIP_YES_LONGSHOTS: bool = _env_bool("TRADING_SKIP_YES_LONGSHOTS", True)
YES_LONGSHOT_MAX_PRICE_CENTS: int = _env_int("TRADING_YES_LONGSHOT_MAX_PRICE_CENTS", 30)

# --- Coverage: market fetch cap ---
# Max markets pulled from Kalshi per ingest. The open-markets feed is dominated
# by a single esports-parlay series; a higher cap reaches more series.
MARKET_FETCH_CAP: int = _env_int("TRADING_MARKET_FETCH_CAP", 3000)

# --- Coverage: event-category ingest ---
# The raw /markets feed is parlay-dominated; the events feed is the only way
# to reach Elections/Politics/Economics markets with their true category.
INGEST_EVENT_CATEGORIES: bool = _env_bool("TRADING_INGEST_EVENT_CATEGORIES", True)
EVENT_FETCH_CAP: int = _env_int("TRADING_EVENT_FETCH_CAP", 2000)

# --- Coverage: Polymarket cross-platform model ---
# Minimum Polymarket volume for a market's price to count as signal, and
# minimum title similarity for a Kalshi↔Polymarket match.
POLYMARKET_MIN_VOLUME_USD: float = _env_float("TRADING_POLYMARKET_MIN_VOLUME_USD", 25_000.0)
POLYMARKET_MIN_SIMILARITY: float = _env_float("TRADING_POLYMARKET_MIN_SIMILARITY", 0.7)

# --- Pre-live hardening: stale data guard ---
# Never score a market whose latest price snapshot is older than this.
# A market that stops getting fresh snapshots has closed early or fallen out
# of the ingest feed — trading on its stale price is trading on fiction.
MAX_SNAPSHOT_AGE_MINUTES: int = _env_int("TRADING_MAX_SNAPSHOT_AGE_MINUTES", 30)

# --- Pre-live hardening: conservative paper fills ---
# Paper trades fill at taker prices (cross the spread) instead of assuming a
# maker fill at bid+1, so paper PnL underestimates rather than overestimates.
PAPER_CONSERVATIVE_FILLS: bool = _env_bool("TRADING_PAPER_CONSERVATIVE_FILLS", True)

# --- Change 5: Order type ---
ORDER_TYPE: str = _env_str("TRADING_ORDER_TYPE", "maker")  # "maker" or "taker"
REQUOTE_SECONDS: int = _env_int("TRADING_REQUOTE_SECONDS", 30)

# --- Change 7: Calibration sample guard ---
MIN_SETTLED_TRADES: int = _env_int("TRADING_MIN_SETTLED_TRADES", 100)

# --- Change 10: Correlation-aware exposure ---
MAX_CLUSTER_EXPOSURE: float = _env_float("TRADING_MAX_CLUSTER_EXPOSURE", 0.10)
