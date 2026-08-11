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

# --- Velocity: max days to expiry ---
# Skip markets resolving further out than this. Capital locked in a months-out
# market can't be recycled; fast-resolving markets turn the bankroll over many
# more times for the same dollars. 0 disables the filter.
MAX_DAYS_TO_EXPIRY: int = _env_int("TRADING_MAX_DAYS_TO_EXPIRY", 14)

# --- Velocity: skip already-held markets ---
# Don't re-enter a market we already hold an open position in. Prevents the
# pipeline from buying the same market every cycle (risk concentration + wasted
# paper-trade count).
SKIP_HELD_MARKETS: bool = _env_bool("TRADING_SKIP_HELD_MARKETS", True)

# --- Cost: Kalshi trading fee (paper settlement realism) ---
# Kalshi charges ceil(rate * contracts * P * (1-P)) per fill. Paper settlement
# subtracts this so paper PnL matches what live would actually net.
KALSHI_FEE_RATE: float = _env_float("TRADING_KALSHI_FEE_RATE", 0.07)

# --- Cost: Odds API quota conservation ---
# A new OddsClient is built each cycle; without a cross-cycle cache every cycle
# re-burns the free monthly quota. TTL caps refresh frequency; the sport list
# trims to in-season leagues. Comma-separated env override.
ODDS_CACHE_TTL_MINUTES: int = _env_int("TRADING_ODDS_CACHE_TTL_MINUTES", 60)
ODDS_SPORT_KEYS: str = _env_str(
    "TRADING_ODDS_SPORT_KEYS",
    "baseball_mlb,basketball_nba,icehockey_nhl",
)

# --- Phase 1.2: odds quota survival ---
# The module-level TTL cache above never applied in production: the pipeline is
# a per-cycle GitHub Actions cron, so every tick got a fresh interpreter and an
# empty cache — ~864 requests/day against a ~500/month allowance. The durable
# cache now lives in the DB, and a ledger stops spending before the API refuses.
ODDS_MONTHLY_QUOTA: int = _env_int("TRADING_ODDS_MONTHLY_QUOTA", 500)
# 0 = derive from the budget: 720h * n_sports / cap (3 sports, 500 cap -> ~4.3h).
ODDS_TTL_MINUTES_OVERRIDE: int = _env_int("TRADING_ODDS_TTL_MINUTES_OVERRIDE", 0)

# Free single-book fallback (ESPN scoreboard, DraftKings only) for when the
# metered provider is dark. Verified live 2026-08-11: moneyline present for
# MLB/NBA/NHL, but ONE book and only on the day of the game — weaker evidence
# than the multi-book consensus, so it is opt-in and confidence-capped.
ENABLE_ESPN_ODDS: bool = _env_bool("TRADING_ENABLE_ESPN_ODDS", False)
ESPN_MAX_CONFIDENCE: float = _env_float("TRADING_ESPN_MAX_CONFIDENCE", 0.70)

# Run the market-only gates (volume/spread/expiry) BEFORE model dispatch, so no
# quota or CPU is spent on a market that cannot qualify. Provably decision-
# neutral (see tests), but it does stop writing Opportunity rows for markets
# that fail those gates, so it is opt-in.
PRESCREEN_BEFORE_MODELS: bool = _env_bool("TRADING_PRESCREEN_BEFORE_MODELS", False)

# --- Coverage: Polymarket scan depth ---
# Polymarket's public API is free and unlimited — scan deeper for more matches.
POLYMARKET_SCAN_LIMIT: int = _env_int("TRADING_POLYMARKET_SCAN_LIMIT", 3000)

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

# --- Phase 1.3: entity-level match verification ---
# Token similarity cannot separate "CPI above 3%" from "CPI below 3%" — same
# words, same numbers, opposite meaning — and that pair was matching in
# production. Titles are now compared field by field (direction, magnitude,
# date, negation, party order) and anything short of a clean match produces no
# estimate and lands in the review queue. ON by default: this is a correction
# to wrong behaviour, and it can only ever REDUCE the set of accepted matches.
POLYMARKET_REQUIRE_ENTITY_MATCH: bool = _env_bool(
    "TRADING_POLYMARKET_REQUIRE_ENTITY_MATCH", True
)

# --- Phase 1.5: resolution-horizon check ---
# Identical titles can still be different questions. Kalshi's "next Prime
# Minister of Israel" runs to 2045; the Polymarket contract of the same name
# force-resolves at the end of 2026. The short-dated probability is bounded
# above by the long-dated one and the gap always points the same way, so
# pricing one off the other manufactures a persistent one-sided "edge" that
# only reveals itself at settlement. Pairs whose horizons differ by more than
# this land in the review queue instead. Tuned against live data: at 90 days
# all 13 currently-matching pairs still price.
POLYMARKET_MAX_HORIZON_GAP_DAYS: int = _env_int(
    "TRADING_POLYMARKET_MAX_HORIZON_GAP_DAYS", 90
)

# --- Phase 2.0: series-targeted ingest ---
# Daily weather contracts are invisible to both existing feeds. Measured
# 2026-08-11: the first 3000 rows of /markets are 1549 General + 1451 Sports
# with zero weather tickers, and the events feed carries only long-horizon
# climate markets. They exist solely behind an explicit series_ticker query.
#
# Config-driven, not hardcoded to the cities that happened to be probed. The
# fetch runs on its own call path and does not touch MARKET_FETCH_CAP or the
# odds quota. Ingesting is not trading — nothing here can place an order.
INGEST_SERIES_TICKERS: str = _env_str(
    "TRADING_INGEST_SERIES_TICKERS",
    "KXHIGHNY,KXHIGHCHI,KXHIGHMIA,KXHIGHDEN,KXHIGHAUS,KXHIGHLAX,KXHIGHPHIL",
)
SERIES_FETCH_CAP: int = _env_int("TRADING_SERIES_FETCH_CAP", 500)


def ingest_series_list() -> list:
    return [s.strip() for s in INGEST_SERIES_TICKERS.split(",") if s.strip()]

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
