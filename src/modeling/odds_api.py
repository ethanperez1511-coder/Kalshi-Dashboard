"""Odds fetching: what to fetch, when, and out of whose budget.

The provider-specific HTTP lives in `odds_sources`; the cross-process cache and
quota ledger live in `odds_store`. This module owns the policy that ties them
together.

History worth keeping: the cache here used to be a module-level dict. That was
correct for a long-running process and became silently useless the moment the
bot moved to a per-cycle GitHub Actions cron — every tick got a fresh
interpreter, so the TTL never applied and ~864 requests/day went out against a
~500/month allowance. The module cache is still here, but only as a
within-cycle shortcut in front of the database.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx

from src.modeling.odds_sources import (
    EspnOddsSource,
    QuotaExhausted,
    TheOddsApiSource,
    _american_to_prob,
)
from src.modeling.odds_store import (
    OddsCacheStore,
    QuotaLedger,
    default_ttl_seconds,
    month_key,
)

logger = logging.getLogger(__name__)

# Default sports keys for major US leagues. The free quota is tiny, so the
# pipeline passes only in-season leagues via TRADING_ODDS_SPORT_KEYS.
SPORT_KEYS = [
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_usa_mls",
    "tennis_atp_french_open",
    "tennis_wta_french_open",
]

# Within-cycle shortcut only. The durable cache is the DB — see odds_store.
# Maps cache-key -> (fetched_at_monotonic, [GameOdds]).
_MODULE_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 3600

# When every source fails, odds this old may still be served rather than going
# dark. These are real observed quotes, not synthetic data; SportsOddsModel
# separately refuses any game that has already commenced, which is the failure
# mode stale odds actually cause.
_MAX_STALE_SECONDS = 12 * 3600


def _clear_module_cache() -> None:
    _MODULE_CACHE.clear()


# Flips True the moment any client sees a quota/auth failure. The pipeline reads
# it once per cycle to alert that the sports model has gone dark.
QUOTA_DEAD = False


@dataclass
class GameOdds:
    """Fair probability for a single game outcome."""
    sport: str
    home_team: str
    away_team: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float  # 0 for non-draw sports
    totals: Dict[str, float]  # e.g. {"over_220.5": 0.52, "under_220.5": 0.48}
    spreads: Dict[str, float]  # e.g. {"Cleveland -5.5": 0.55}
    commence_time: str
    # Provenance. A single-book quote is weaker evidence than a multi-book
    # consensus, and the consuming model discounts its confidence accordingly.
    source: str = "the_odds_api"
    book_count: int = 0


def _remove_vig(probs: List[float]) -> List[float]:
    """Remove vigorish by normalizing probabilities to sum to 1."""
    total = sum(probs)
    if total == 0:
        return probs
    return [p / total for p in probs]


def devig_two_way(p_yes: float, p_no: float) -> tuple:
    """De-vig a two-way market: normalize so outcomes sum to 1.

    Given implied probs that include vig (sum > 1), returns
    (devigged_yes, devigged_no) summing to 1.
    """
    total = p_yes + p_no
    if total == 0:
        return (0.5, 0.5)
    return (p_yes / total, p_no / total)


def devig_book_then_average(book_outcomes: List[List[float]]) -> List[float]:
    """De-vig each bookmaker separately, then average across books.

    book_outcomes: list of [p_yes, p_no] pairs from each bookmaker.
    Returns [avg_devigged_yes, avg_devigged_no].
    """
    if not book_outcomes:
        return [0.5, 0.5]
    devigged = []
    for outcomes in book_outcomes:
        if len(outcomes) == 2:
            dy, dn = devig_two_way(outcomes[0], outcomes[1])
            devigged.append((dy, dn))
    if not devigged:
        return [0.5, 0.5]
    avg_yes = sum(d[0] for d in devigged) / len(devigged)
    avg_no = sum(d[1] for d in devigged) / len(devigged)
    return [avg_yes, avg_no]


class OddsClient:
    def __init__(
        self,
        api_key: str,
        sport_keys: Optional[List[str]] = None,
        ttl_seconds: Optional[int] = None,
        http=httpx,
        engine=None,
        now: Optional[Callable[[], datetime]] = None,
        monthly_cap: Optional[int] = None,
        enable_espn: Optional[bool] = None,
    ):
        from src.trading_config import ENABLE_ESPN_ODDS, ODDS_MONTHLY_QUOTA

        self._api_key = api_key
        self._sport_keys = sport_keys if sport_keys is not None else SPORT_KEYS
        self._http = http
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cap = monthly_cap if monthly_cap is not None else ODDS_MONTHLY_QUOTA
        self._enable_espn = ENABLE_ESPN_ODDS if enable_espn is None else enable_espn
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else default_ttl_seconds(len(self._sport_keys), self._cap)
        )
        self._store = OddsCacheStore(engine) if engine is not None else None
        self._ledger = QuotaLedger(engine, cap=self._cap) if engine is not None else None
        # True once a quota/auth failure is seen — lets the pipeline alert that
        # the sports model has gone dark instead of failing silently.
        self.quota_dead = False

    # -- sources ---------------------------------------------------------

    def _primary(self) -> Optional[TheOddsApiSource]:
        return TheOddsApiSource(self._api_key, http=self._http) if self._api_key else None

    def _fallback(self) -> Optional[EspnOddsSource]:
        if not self._enable_espn:
            return None
        return EspnOddsSource(http=self._http, now=self._now)

    # -- cache -----------------------------------------------------------

    def _cache_key(self) -> str:
        return f"{self._api_key}|{','.join(sorted(self._sport_keys))}"

    def clear_cache(self):
        _MODULE_CACHE.pop(self._cache_key(), None)

    def _from_store(self, sport_key: str, now: datetime, allow_stale: bool = False):
        """Parsed games from the DB cache, or None if absent/too old."""
        if self._store is None:
            return None
        cached = self._store.get(sport_key)
        if cached is None:
            return None
        fetched_at, payload, _source = cached
        age = (now - fetched_at).total_seconds()
        limit = _MAX_STALE_SECONDS if allow_stale else self._ttl
        if age >= limit:
            return None
        if allow_stale and age >= self._ttl:
            logger.warning(
                f"Serving {sport_key} odds {age / 3600:.1f}h old — every source failed"
            )
        return [GameOdds(**g) for g in payload]

    # -- fetching --------------------------------------------------------

    def get_all_odds(self) -> List[GameOdds]:
        """Odds for the configured sports, spending as little quota as possible."""
        now = self._now()
        key = self._cache_key()
        cached = _MODULE_CACHE.get(key)
        if cached is not None:
            fetched_at, games = cached
            if (time.monotonic() - fetched_at) < self._ttl:
                return games

        all_games: List[GameOdds] = []
        for sport_key in self._sport_keys:
            all_games.extend(self._odds_for_sport(sport_key, now))

        if not self.quota_dead:
            _MODULE_CACHE[key] = (time.monotonic(), all_games)
        return all_games

    def _odds_for_sport(self, sport_key: str, now: datetime) -> List[GameOdds]:
        fresh = self._from_store(sport_key, now)
        if fresh is not None:
            logger.debug(f"Odds cache hit for {sport_key} — no request spent")
            return fresh

        for source in (self._primary(), self._fallback()):
            if source is None:
                continue
            if source.costs_quota and not self._may_spend(source.name, now):
                continue
            try:
                games = self._fetch(source, sport_key, now)
            except QuotaExhausted:
                logger.warning(
                    f"{source.name}: quota exhausted or key invalid — marking dark"
                )
                self._mark_quota_dead(source.name, now)
                continue
            except Exception:
                logger.warning(f"{source.name}: fetch failed for {sport_key}", exc_info=True)
                continue
            if games:
                return games

        # Every source failed. Real-but-stale beats going dark; the model still
        # refuses any game that has already started.
        stale = self._from_store(sport_key, now, allow_stale=True)
        return stale or []

    def _may_spend(self, source_name: str, now: datetime) -> bool:
        if self._ledger is None:
            return True
        if self._ledger.remaining(month_key(now), source_name) <= 0:
            logger.warning(f"{source_name}: monthly quota spent — not requesting")
            self._mark_quota_dead(source_name, now, exhaust_ledger=False)
            return False
        return True

    def _fetch(self, source, sport_key: str, now: datetime) -> List[GameOdds]:
        if source.costs_quota and self._ledger is not None:
            # Charge before the call: an over-count costs one skipped refresh,
            # an under-count costs a month of blindness.
            self._ledger.charge(month_key(now), source.name, 1)

        raw = source.fetch(sport_key)
        games = (
            self._parse_response(raw, sport_key)
            if source.name == "the_odds_api"
            else list(raw)
        )
        if games and self._store is not None:
            self._store.put(sport_key, source.name, [asdict(g) for g in games], now)
        return games

    def _mark_quota_dead(self, source_name: str, now: datetime, exhaust_ledger: bool = True):
        self.quota_dead = True
        global QUOTA_DEAD
        QUOTA_DEAD = True
        if exhaust_ledger and self._ledger is not None:
            # The provider says we are done for the month; stop asking.
            month = month_key(now)
            spent = self._ledger.used(month, source_name)
            if spent < self._cap:
                self._ledger.charge(month, source_name, self._cap - spent)

    # -- the-odds-api parsing --------------------------------------------

    def _parse_response(self, data: list, sport_key: str) -> List[GameOdds]:
        games: List[GameOdds] = []
        for event in data:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            commence = event.get("commence_time", "")

            h2h_probs = self._extract_h2h(event)
            totals = self._extract_totals(event)
            spreads = self._extract_spreads(event)

            games.append(GameOdds(
                sport=sport_key,
                home_team=home,
                away_team=away,
                home_win_prob=h2h_probs.get(home, 0.5),
                away_win_prob=h2h_probs.get(away, 0.5),
                draw_prob=h2h_probs.get("Draw", 0.0),
                totals=totals,
                spreads=spreads,
                commence_time=commence,
                source="the_odds_api",
                book_count=len(event.get("bookmakers", [])),
            ))
        logger.debug(f"Odds API: {len(games)} games for {sport_key}")
        return games

    def _extract_h2h(self, event: dict) -> Dict[str, float]:
        """De-vig each bookmaker separately, then average across books."""
        per_book: List[Dict[str, float]] = []
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "h2h":
                    continue
                book_probs: Dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    book_probs[outcome["name"]] = _american_to_prob(outcome["price"])
                if book_probs:
                    total = sum(book_probs.values())
                    if total > 0:
                        book_probs = {k: v / total for k, v in book_probs.items()}
                    per_book.append(book_probs)

        if not per_book:
            return {}

        all_names = set()
        for bp in per_book:
            all_names.update(bp.keys())

        avg_probs: Dict[str, float] = {}
        for name in all_names:
            vals = [bp[name] for bp in per_book if name in bp]
            avg_probs[name] = sum(vals) / len(vals) if vals else 0.0
        return avg_probs

    def _extract_totals(self, event: dict) -> Dict[str, float]:
        """Extract over/under totals, de-vig per book then average."""
        per_book_by_point: Dict[float, List[Dict[str, float]]] = {}
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "totals":
                    continue
                book_points: Dict[float, Dict[str, float]] = {}
                for outcome in market.get("outcomes", []):
                    point = outcome.get("point", 0)
                    name = outcome["name"].lower()  # "over" or "under"
                    book_points.setdefault(point, {})[name] = _american_to_prob(
                        outcome["price"]
                    )
                for point, probs in book_points.items():
                    if "over" in probs and "under" in probs:
                        total = probs["over"] + probs["under"]
                        if total > 0:
                            probs["over"] /= total
                            probs["under"] /= total
                    per_book_by_point.setdefault(point, []).append(probs)

        result: Dict[str, float] = {}
        for point, book_list in per_book_by_point.items():
            for direction in ("over", "under"):
                vals = [bp[direction] for bp in book_list if direction in bp]
                if vals:
                    result[f"{direction}_{point}"] = sum(vals) / len(vals)
        return result

    def _extract_spreads(self, event: dict) -> Dict[str, float]:
        """Extract spread odds, averaged across books."""
        spread_odds: Dict[str, List[float]] = {}
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "spreads":
                    continue
                for outcome in market.get("outcomes", []):
                    point = outcome.get("point", 0)
                    key = f"{outcome['name']} {point:+.1f}"
                    spread_odds.setdefault(key, []).append(
                        _american_to_prob(outcome["price"])
                    )
        return {k: sum(v) / len(v) for k, v in spread_odds.items()}
