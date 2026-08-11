"""Pluggable odds providers.

`OddsClient` orchestrates *when* to fetch (cache TTL, quota budget); a source
knows only *how* to fetch one sport and hand back `GameOdds`. That split is what
lets a free fallback take over when the metered provider goes dark.

Sources return `[]` for "nothing available" and raise for "the request failed" —
the caller must be able to tell a quiet slate from a broken feed, because only
the second one should keep a stale cache entry alive.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# ESPN organises by sport/league path, not by the Odds API's flat keys.
_ESPN_PATHS: Dict[str, str] = {
    "baseball_mlb": "baseball/mlb",
    "basketball_nba": "basketball/nba",
    "icehockey_nhl": "hockey/nhl",
    "americanfootball_nfl": "football/nfl",
}


class QuotaExhausted(RuntimeError):
    """The provider refused us for quota or auth reasons (401/429)."""


class OddsSource(Protocol):
    name: str
    costs_quota: bool

    def fetch(self, sport_key: str) -> List["GameOdds"]:  # noqa: F821
        ...


def _american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


class TheOddsApiSource:
    """the-odds-api.com — multi-book, metered (~500 requests/month free)."""

    name = "the_odds_api"
    costs_quota = True

    def __init__(self, api_key: str, http=httpx):
        self._api_key = api_key
        self._http = http

    def fetch(self, sport_key: str) -> List[dict]:
        """Return the provider's raw event list, or raise.

        Raw JSON rather than parsed objects: the cache stores the payload
        verbatim so parsing can be revised later without re-spending quota.
        """
        resp = self._http.get(
            f"{_ODDS_API_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": self._api_key,
                "regions": "us",
                "markets": "h2h,totals,spreads",
                "oddsFormat": "american",
            },
            timeout=15.0,
        )
        if resp.status_code in (401, 429):
            raise QuotaExhausted(
                f"Odds API refused {sport_key}: {resp.status_code}"
            )
        if resp.status_code == 422:
            logger.debug(f"Odds API: sport {sport_key} not available")
            return []
        if resp.status_code != 200:
            raise RuntimeError(f"Odds API {sport_key} returned {resp.status_code}")
        return resp.json()


class EspnOddsSource:
    """ESPN's public scoreboard — free, no key, but ONE book (DraftKings).

    Verified live 2026-08-11: 15/15 same-day MLB events carried a moneyline;
    NBA/NHL share the shape when in season. Three constraints drove this
    implementation and are covered by tests:

    * The undated endpoint returns *yesterday's* slate, so `?dates=` is always
      sent explicitly.
    * Once a game is FINAL the `odds` key is dropped entirely (not emptied), so
      a missing key is a skip, never an error.
    * `site.api.espn.com` sits behind an Akamai bot manager that 403s
      browser-like and custom User-Agents while passing library defaults —
      so no headers are set here on purpose.

    Because there is only one book, cross-book de-vig is impossible: we
    normalise DraftKings' own two-way price and mark `book_count=1` so the
    consuming model can discount its confidence accordingly.
    """

    name = "espn"
    costs_quota = False

    def __init__(self, http=httpx, now: Optional[Callable[[], datetime]] = None):
        self._http = http
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, sport_key: str) -> List["GameOdds"]:  # noqa: F821
        from src.modeling.odds_api import GameOdds  # circular at module level

        path = _ESPN_PATHS.get(sport_key)
        if path is None:
            logger.debug(f"ESPN: no league path for {sport_key}")
            return []

        resp = self._http.get(
            f"{_ESPN_BASE}/{path}/scoreboard",
            params={"dates": self._now().strftime("%Y%m%d")},
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ESPN {sport_key} returned {resp.status_code}")

        games: List[GameOdds] = []
        for event in resp.json().get("events", []):
            for comp in event.get("competitions", []):
                game = self._parse_competition(comp, sport_key)
                if game is not None:
                    games.append(game)
        logger.debug(f"ESPN: {len(games)} games with odds for {sport_key}")
        return games

    def _parse_competition(self, comp: dict, sport_key: str):
        from src.modeling.odds_api import GameOdds

        odds_list = comp.get("odds") or []
        if not odds_list:
            return None  # FINAL games drop the key entirely
        odds = odds_list[0]

        teams = {
            c.get("homeAway"): c.get("team", {}).get("displayName", "")
            for c in comp.get("competitors", [])
        }
        home, away = teams.get("home", ""), teams.get("away", "")
        if not home or not away:
            return None

        ml = odds.get("moneyline") or {}
        home_odds = _espn_ml(ml.get("home"))
        away_odds = _espn_ml(ml.get("away"))
        if home_odds is None or away_odds is None:
            return None

        p_home = _american_to_prob(home_odds)
        p_away = _american_to_prob(away_odds)
        total = p_home + p_away
        if total <= 0:
            return None
        # Single book, so this de-vig inherits DraftKings' own juice asymmetry.
        p_home, p_away = p_home / total, p_away / total

        totals = {}
        over_under = odds.get("overUnder")
        if over_under is not None:
            # No per-side prices in this feed — record the line, not a fake price.
            totals[f"line_{over_under}"] = 0.0

        return GameOdds(
            sport=sport_key,
            home_team=home,
            away_team=away,
            home_win_prob=p_home,
            away_win_prob=p_away,
            draw_prob=0.0,
            totals=totals,
            spreads={},
            commence_time=comp.get("date", ""),
            source="espn",
            book_count=1,
        )


def _espn_ml(side: Optional[dict]) -> Optional[int]:
    """Pull an American-odds int from ESPN's signed-string moneyline block.

    Prefers the closing line and falls back to the open. The scoreboard feed
    stores these as strings ("-123"/"+114") while the summary and core feeds use
    ints — this only ever sees the scoreboard shape.
    """
    if not side:
        return None
    for phase in ("close", "current", "open"):
        raw = (side.get(phase) or {}).get("odds")
        if raw in (None, ""):
            continue
        try:
            return int(str(raw).replace("+", "").strip())
        except ValueError:
            continue
    return None
