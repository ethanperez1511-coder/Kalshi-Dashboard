"""Client for The Odds API (the-odds-api.com) — free tier."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.the-odds-api.com/v4"

# Default sports keys for major US leagues. The free quota is tiny (500/mo), so
# the pipeline passes only in-season leagues via TRADING_ODDS_SPORT_KEYS.
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

# Module-level cache shared across OddsClient instances (a fresh client is built
# every pipeline cycle). Without this, every cycle re-burns the monthly quota.
# Maps cache-key -> (fetched_at_monotonic, [GameOdds]).
_MODULE_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 3600  # overridden per-client below


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


def _american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


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


def devig_book_then_average(
    book_outcomes: List[List[float]],
) -> List[float]:
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
        ttl_seconds: int = _CACHE_TTL_SECONDS,
        http=httpx,
    ):
        self._api_key = api_key
        self._sport_keys = sport_keys if sport_keys is not None else SPORT_KEYS
        self._ttl = ttl_seconds
        self._http = http
        # True once a 401/quota response is seen — lets the pipeline alert that
        # the sports model has gone dark instead of failing silently.
        self.quota_dead = False

    def _cache_key(self) -> str:
        return f"{self._api_key}|{','.join(sorted(self._sport_keys))}"

    def get_all_odds(self) -> List[GameOdds]:
        """Fetch odds for the configured sports, served from a TTL cache.

        The cache spans client instances so consecutive pipeline cycles don't
        each spend the monthly quota.
        """
        key = self._cache_key()
        cached = _MODULE_CACHE.get(key)
        if cached is not None:
            fetched_at, games = cached
            if (time.monotonic() - fetched_at) < self._ttl:
                return games

        all_games: List[GameOdds] = []
        for sport_key in self._sport_keys:
            all_games.extend(self._fetch_sport(sport_key))

        # Only cache a successful fetch — caching an empty quota-dead result
        # would hide recovery when the quota resets.
        if not self.quota_dead:
            _MODULE_CACHE[key] = (time.monotonic(), all_games)
        return all_games

    def clear_cache(self):
        _MODULE_CACHE.pop(self._cache_key(), None)

    def _fetch_sport(self, sport_key: str) -> List[GameOdds]:
        """Fetch moneyline + totals + spreads for a sport."""
        try:
            resp = self._http.get(
                f"{_BASE_URL}/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us",
                    "markets": "h2h,totals,spreads",
                    "oddsFormat": "american",
                },
                timeout=15.0,
            )
            if resp.status_code in (401, 429):
                logger.warning(f"Odds API: quota exhausted or invalid key ({resp.status_code})")
                self.quota_dead = True
                global QUOTA_DEAD
                QUOTA_DEAD = True
                return []
            if resp.status_code == 422:
                logger.debug(f"Odds API: sport {sport_key} not available")
                return []
            if resp.status_code != 200:
                logger.warning(f"Odds API: {sport_key} returned {resp.status_code}")
                return []
            return self._parse_response(resp.json(), sport_key)
        except httpx.HTTPError as e:
            logger.warning(f"Odds API error for {sport_key}: {e}")
            return []

    def _parse_response(self, data: list, sport_key: str) -> List[GameOdds]:
        games: List[GameOdds] = []
        for event in data:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            commence = event.get("commence_time", "")

            h2h_probs = self._extract_h2h(event)
            totals = self._extract_totals(event)
            spreads = self._extract_spreads(event)

            home_prob = h2h_probs.get(home, 0.5)
            away_prob = h2h_probs.get(away, 0.5)
            draw_prob = h2h_probs.get("Draw", 0.0)

            games.append(GameOdds(
                sport=sport_key,
                home_team=home,
                away_team=away,
                home_win_prob=home_prob,
                away_win_prob=away_prob,
                draw_prob=draw_prob,
                totals=totals,
                spreads=spreads,
                commence_time=commence,
            ))
        logger.debug(f"Odds API: {len(games)} games for {sport_key}")
        return games

    def _extract_h2h(self, event: dict) -> Dict[str, float]:
        """De-vig each bookmaker separately, then average across books."""
        # Collect per-book outcome sets: [{name: prob}, ...]
        per_book: List[Dict[str, float]] = []
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "h2h":
                    continue
                book_probs: Dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    book_probs[outcome["name"]] = _american_to_prob(outcome["price"])
                if book_probs:
                    # De-vig this book: normalize to sum to 1
                    total = sum(book_probs.values())
                    if total > 0:
                        book_probs = {k: v / total for k, v in book_probs.items()}
                    per_book.append(book_probs)

        if not per_book:
            return {}

        # Average de-vigged probs across books
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
        # Collect per-book over/under pairs grouped by point
        # Structure: {point: [{over: prob, under: prob}, ...]}
        per_book_by_point: Dict[float, List[Dict[str, float]]] = {}
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market["key"] != "totals":
                    continue
                # Group outcomes by point within this book
                book_points: Dict[float, Dict[str, float]] = {}
                for outcome in market.get("outcomes", []):
                    point = outcome.get("point", 0)
                    name = outcome["name"].lower()  # "over" or "under"
                    prob = _american_to_prob(outcome["price"])
                    book_points.setdefault(point, {})[name] = prob
                # De-vig each pair
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
                    name = outcome["name"]
                    key = f"{name} {point:+.1f}"
                    prob = _american_to_prob(outcome["price"])
                    spread_odds.setdefault(key, []).append(prob)

        result: Dict[str, float] = {}
        for key, probs in spread_odds.items():
            result[key] = sum(probs) / len(probs)
        return result
