"""Sports model that compares Kalshi parlay prices to external sportsbook odds."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Engine

from src.modeling.base import BaseModel, ModelResult, MODEL_TYPE_INDEPENDENT
from src.modeling.odds_api import GameOdds, OddsClient
from src.trading_config import ESPN_MAX_CONFIDENCE, SKIP_SAME_GAME_PARLAYS

logger = logging.getLogger(__name__)

# Known NBA/MLB/NHL team short names → full names
_TEAM_ALIASES: Dict[str, List[str]] = {
    # NBA
    "Cleveland": ["Cleveland Cavaliers"],
    "Boston": ["Boston Celtics", "Boston Red Sox"],
    "New York": ["New York Knicks", "New York Yankees", "New York Mets", "New York Rangers"],
    "New York Y": ["New York Yankees"],
    "New York M": ["New York Mets"],
    "Oklahoma City": ["Oklahoma City Thunder"],
    "San Antonio": ["San Antonio Spurs"],
    "Detroit": ["Detroit Pistons", "Detroit Tigers"],
    "Chicago": ["Chicago Bulls", "Chicago Cubs"],
    "Chicago WS": ["Chicago White Sox"],
    "LA Lakers": ["Los Angeles Lakers"],
    "LA Clippers": ["Los Angeles Clippers"],
    "Minnesota": ["Minnesota Timberwolves", "Minnesota Twins"],
    "Denver": ["Denver Nuggets", "Colorado Rockies"],
    "Dallas": ["Dallas Mavericks"],
    "Miami": ["Miami Heat", "Miami Marlins"],
    "Milwaukee": ["Milwaukee Bucks", "Milwaukee Brewers"],
    "Philadelphia": ["Philadelphia 76ers", "Philadelphia Phillies"],
    "Phoenix": ["Phoenix Suns"],
    "Toronto": ["Toronto Raptors", "Toronto Blue Jays"],
    "Indiana": ["Indiana Pacers"],
    "Golden State": ["Golden State Warriors"],
    "Houston": ["Houston Rockets", "Houston Astros"],
    "Sacramento": ["Sacramento Kings"],
    "Portland": ["Portland Trail Blazers"],
    "Memphis": ["Memphis Grizzlies"],
    "Charlotte": ["Charlotte Hornets"],
    "Washington": ["Washington Wizards", "Washington Nationals"],
    "Atlanta": ["Atlanta Hawks", "Atlanta Braves"],
    "Orlando": ["Orlando Magic"],
    "Brooklyn": ["Brooklyn Nets"],
    # MLB
    "Texas": ["Texas Rangers"],
    "San Diego": ["San Diego Padres"],
    "San Francisco": ["San Francisco Giants"],
    "Kansas City": ["Kansas City Royals"],
    "St. Louis": ["St. Louis Cardinals"],
    "Baltimore": ["Baltimore Orioles"],
    "Tampa Bay": ["Tampa Bay Rays"],
    "Seattle": ["Seattle Mariners"],
    "Los Angeles": ["Los Angeles Dodgers", "Los Angeles Angels", "Los Angeles Lakers"],
    "Pittsburgh": ["Pittsburgh Pirates"],
    "Cincinnati": ["Cincinnati Reds"],
    "Arizona": ["Arizona Diamondbacks"],
    "Colorado": ["Colorado Rockies"],
    # Soccer
    "Manchester City": ["Manchester City"],
    "Manchester United": ["Manchester United"],
    "Liverpool": ["Liverpool"],
    "Arsenal": ["Arsenal"],
    "Chelsea": ["Chelsea"],
}


@dataclass
class ParlayLeg:
    """A single leg parsed from a Kalshi MVE market title."""
    raw: str
    side: str  # "yes" or "no"
    leg_type: str  # "moneyline", "player_prop", "total", "spread", "unknown"
    team: str  # team name for moneyline/spread
    player: str  # player name for props
    stat_threshold: float  # e.g. 30 for "30+ points"
    total_line: float  # e.g. 220.5 for "Over 220.5 points"
    spread_line: float  # e.g. 5.5 for "wins by over 5.5"
    over_under: str  # "over" or "under" for totals


def parse_legs(title: str) -> List[ParlayLeg]:
    """Parse comma-separated parlay legs from a Kalshi MVE title."""
    parts = [p.strip() for p in title.split(",")]
    legs: List[ParlayLeg] = []
    for part in parts:
        leg = _parse_single_leg(part)
        if leg:
            legs.append(leg)
    return legs


def _parse_single_leg(text: str) -> Optional[ParlayLeg]:
    """Parse a single leg like 'yes Cleveland' or 'yes Donovan Mitchell: 30+'."""
    text = text.strip()
    if not text:
        return None

    # Extract side
    side = "yes"
    body = text
    if text.lower().startswith("yes "):
        side = "yes"
        body = text[4:]
    elif text.lower().startswith("no "):
        side = "no"
        body = text[3:]

    # Check: player prop like "Donovan Mitchell: 30+" or "James Harden: 8+"
    prop_match = re.match(r"^(.+?):\s*(\d+(?:\.\d+)?)\+?$", body)
    if prop_match:
        return ParlayLeg(
            raw=text, side=side, leg_type="player_prop",
            team="", player=prop_match.group(1).strip(),
            stat_threshold=float(prop_match.group(2)),
            total_line=0, spread_line=0, over_under="",
        )

    # Check: total like "Over 220.5 points scored"
    total_match = re.match(r"^(Over|Under)\s+(\d+(?:\.\d+)?)\s+(points|runs)", body, re.IGNORECASE)
    if total_match:
        return ParlayLeg(
            raw=text, side=side, leg_type="total",
            team="", player="",
            stat_threshold=0, total_line=float(total_match.group(2)),
            spread_line=0, over_under=total_match.group(1).lower(),
        )

    # Check: spread like "Cleveland wins by over 5.5 points"
    spread_match = re.match(r"^(.+?)\s+wins\s+by\s+over\s+(\d+(?:\.\d+)?)\s+(points|runs)", body, re.IGNORECASE)
    if spread_match:
        return ParlayLeg(
            raw=text, side=side, leg_type="spread",
            team=spread_match.group(1).strip(), player="",
            stat_threshold=0, total_line=0,
            spread_line=float(spread_match.group(2)),
            over_under="",
        )

    # Check: moneyline — just a team name
    if body and not any(c.isdigit() for c in body):
        return ParlayLeg(
            raw=text, side=side, leg_type="moneyline",
            team=body.strip(), player="",
            stat_threshold=0, total_line=0, spread_line=0, over_under="",
        )

    return ParlayLeg(
        raw=text, side=side, leg_type="unknown",
        team="", player="",
        stat_threshold=0, total_line=0, spread_line=0, over_under="",
    )


def _find_game(team: str, games: List[GameOdds]) -> Optional[GameOdds]:
    """Find a game matching a team name."""
    aliases = _TEAM_ALIASES.get(team, [team])
    for game in games:
        for alias in aliases:
            a = alias.lower()
            if a in game.home_team.lower() or a in game.away_team.lower():
                return game
    return None


def _team_win_prob(team: str, game: GameOdds) -> float:
    """Get win probability for a team from game odds."""
    aliases = _TEAM_ALIASES.get(team, [team])
    for alias in aliases:
        a = alias.lower()
        if a in game.home_team.lower():
            return game.home_win_prob
        if a in game.away_team.lower():
            return game.away_win_prob
    return 0.5


def _game_commenced(game: GameOdds) -> bool:
    """True if the game has started (or its start time can't be parsed).

    Pre-game odds for a live or finished game are stale — matching them to a
    market would price on fiction, so an unparseable timestamp fails safe.
    """
    raw = (game.commence_time or "").replace("Z", "+00:00")
    try:
        start = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start <= datetime.now(timezone.utc)


def _leg_probability(leg: ParlayLeg, games: List[GameOdds]) -> Optional[float]:
    """Estimate probability for a single parlay leg."""
    if leg.leg_type == "moneyline":
        game = _find_game(leg.team, games)
        if game is None:
            return None
        prob = _team_win_prob(leg.team, game)
        if leg.side == "no":
            prob = 1.0 - prob
        return prob

    elif leg.leg_type == "total":
        # Find a game with matching total line
        key = f"{leg.over_under}_{leg.total_line}"
        for game in games:
            if key in game.totals:
                prob = game.totals[key]
                if leg.side == "no":
                    prob = 1.0 - prob
                return prob
        return None

    elif leg.leg_type == "spread":
        game = _find_game(leg.team, games)
        if game is None:
            return None
        # Look for matching spread
        for spread_key, prob in game.spreads.items():
            if leg.team.lower() in spread_key.lower():
                # Check if spread line roughly matches
                match = re.search(r"([+-]?\d+\.?\d*)", spread_key)
                if match:
                    spread_val = abs(float(match.group(1)))
                    if abs(spread_val - leg.spread_line) < 1.5:
                        if leg.side == "no":
                            prob = 1.0 - prob
                        return prob
        return None

    elif leg.leg_type == "player_prop":
        # The Odds API free tier carries no player-prop markets. There is no
        # real data behind a prop leg, so it never gets a probability.
        return None

    return None


class SportsOddsModel(BaseModel):
    """Estimate parlay probability by combining per-leg odds from sportsbooks."""

    def __init__(self, odds_client: Optional[OddsClient] = None):
        self._odds_client = odds_client
        self._games: Optional[List[GameOdds]] = None

    @property
    def category(self) -> str:
        return "Sports"

    @property
    def model_type(self) -> str:
        return MODEL_TYPE_INDEPENDENT

    def _get_games(self) -> List[GameOdds]:
        if self._games is not None:
            return self._games
        if self._odds_client is None:
            return []
        self._games = self._odds_client.get_all_odds()
        logger.info(f"SportsOddsModel: loaded {len(self._games)} games from odds API")
        return self._games

    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        games = self._get_games()
        # Only games that haven't started: pre-game odds for a live or
        # finished game are stale and must never be matched.
        games = [g for g in games if not _game_commenced(g)]
        if not games:
            return None

        legs = parse_legs(title)
        if not legs:
            return None

        leg_probs: List[Tuple[ParlayLeg, float]] = []

        for leg in legs:
            prob = _leg_probability(leg, games)
            if prob is None:
                # Any leg without real external data kills the estimate.
                # Substituting a neutral prior here manufactures fake edges.
                return None
            leg_probs.append((leg, prob))

        # --- Same-game parlay detection ---
        # Map each leg to its matched game (if any) for correlation check.
        leg_games: List[Optional[GameOdds]] = []
        for leg, _ in leg_probs:
            if leg.leg_type in ("moneyline", "spread"):
                leg_games.append(_find_game(leg.team, games))
            elif leg.leg_type == "total":
                # Totals aren't team-specific; mark as None
                leg_games.append(None)
            elif leg.leg_type == "player_prop":
                leg_games.append(None)
            else:
                leg_games.append(None)

        # Check for shared games among legs
        game_ids = [id(g) for g in leg_games if g is not None]
        is_same_game_parlay = len(game_ids) != len(set(game_ids))

        if is_same_game_parlay and SKIP_SAME_GAME_PARLAYS:
            logger.info(
                f"Skipping same-game parlay {market_id}: legs share a game event. "
                "Leg independence assumption violated."
            )
            return None

        # --- HOOK: future correlation model ---
        # If is_same_game_parlay is True but SKIP_SAME_GAME_PARLAYS is False,
        # a correlation adjustment should be applied here before multiplying.
        # For now we multiply assuming independence (which is wrong for SGPs).
        # TODO: implement correlation_adjustment(leg_probs, leg_games)

        # Parlay probability = product of independent leg probabilities.
        # Every leg is backed by external odds — partial matches returned None above.
        parlay_prob = 1.0
        for _, prob in leg_probs:
            parlay_prob *= prob

        # Confidence tracks the weakest source any leg leaned on. A multi-book
        # consensus earns the full 0.85; the ESPN fallback carries DraftKings
        # only, so it cannot be de-vigged across books and inherits that one
        # book's juice asymmetry — cap it well below.
        sources = {g.source for g in leg_games if g is not None} or {"the_odds_api"}
        confidence = 0.85
        if sources != {"the_odds_api"}:
            confidence = min(confidence, ESPN_MAX_CONFIDENCE)

        leg_details = [
            f"{leg.leg_type}({prob:.0%},ext)" for leg, prob in leg_probs
        ]

        return ModelResult(
            market_id=market_id,
            p_model=parlay_prob,
            confidence=confidence,
            reasoning=(
                f"Parlay {len(legs)} legs: {' × '.join(leg_details)} "
                f"= {parlay_prob:.4f}; matched {len(legs)}/{len(legs)} legs "
                f"[{','.join(sorted(sources))}]"
            ),
            data_sources=sorted(sources),
        )
