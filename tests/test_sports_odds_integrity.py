"""Integrity tests for SportsOddsModel — no fabricated probabilities.

Bugs these tests pin down (found 2026-06-12):
1. Player-prop legs were scored by a made-up exponential curve (hardcoded NBA
   rate applied to MLB hits / NHL points) and labeled "ext". The Odds API free
   tier has no player-prop data — any number here is fiction.
2. Unmatched legs were silently substituted with 0.5, manufacturing huge fake
   edges in both directions.
3. Games already commenced were still matched, pairing pre-game odds with
   live/finished markets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.modeling.models.sports_odds import (
    SportsOddsModel,
    ParlayLeg,
    _leg_probability,
    parse_legs,
)
from src.modeling.odds_api import GameOdds


def _future_iso(hours: int = 6) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _past_iso(hours: int = 6) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _game(
    home="Cleveland Cavaliers",
    away="Boston Celtics",
    home_prob=0.6,
    away_prob=0.4,
    commence=None,
) -> GameOdds:
    return GameOdds(
        sport="basketball_nba",
        home_team=home,
        away_team=away,
        home_win_prob=home_prob,
        away_win_prob=away_prob,
        draw_prob=0.0,
        totals={},
        spreads={},
        commence_time=commence or _future_iso(),
    )


class _FakeOddsClient:
    def __init__(self, games):
        self._games = games

    def get_all_odds(self):
        return self._games


class TestPlayerPropsNeverFabricated:
    def test_player_prop_leg_returns_none(self):
        """No external prop data exists — a prop leg must not get a number."""
        leg = ParlayLeg(
            raw="yes Adley Rutschman: 1+", side="yes", leg_type="player_prop",
            team="", player="Adley Rutschman", stat_threshold=1.0,
            total_line=0, spread_line=0, over_under="",
        )
        assert _leg_probability(leg, [_game()]) is None

    def test_all_prop_parlay_returns_no_estimate(self):
        """The +85% 'edge' case: 3 prop legs scored 95% each vs market at 1%."""
        model = SportsOddsModel(_FakeOddsClient([_game()]))
        result = model.estimate(
            market_id="M1",
            title="yes Adley Rutschman: 1+,yes Julio Rodriguez: 1+,yes Michael Harris: 1+",
            current_price=1.0,
            engine=None,
        )
        assert result is None


class TestNoNeutralSubstitution:
    def test_unmatched_leg_kills_estimate(self):
        """One matched moneyline + one unmatched leg: no estimate, not 0.5 filler."""
        games = [_game()]  # only Cleveland/Boston exists
        model = SportsOddsModel(_FakeOddsClient(games))
        result = model.estimate(
            market_id="M2",
            title="yes Cleveland,yes Over 4.5 runs scored",  # total not in any game
            current_price=40.0,
            engine=None,
        )
        assert result is None

    def test_all_matched_moneylines_produce_estimate(self):
        games = [
            _game(home="Cleveland Cavaliers", away="Orlando Magic",
                  home_prob=0.7, away_prob=0.3),
            _game(home="Dallas Mavericks", away="Phoenix Suns",
                  home_prob=0.55, away_prob=0.45),
        ]
        model = SportsOddsModel(_FakeOddsClient(games))
        result = model.estimate(
            market_id="M3",
            title="yes Cleveland,yes Dallas",
            current_price=38.0,
            engine=None,
        )
        assert result is not None
        assert abs(result.p_model - 0.7 * 0.55) < 1e-9
        assert 0.0 <= result.p_model <= 1.0


class TestCommencedGamesSkipped:
    def test_commenced_game_not_matched(self):
        """Pre-game odds for a live/finished game are stale — never match them."""
        games = [_game(commence=_past_iso())]
        model = SportsOddsModel(_FakeOddsClient(games))
        result = model.estimate(
            market_id="M4", title="yes Cleveland", current_price=50.0, engine=None,
        )
        assert result is None

    def test_future_game_still_matched(self):
        games = [_game(commence=_future_iso())]
        model = SportsOddsModel(_FakeOddsClient(games))
        result = model.estimate(
            market_id="M5", title="yes Cleveland", current_price=50.0, engine=None,
        )
        assert result is not None
        assert abs(result.p_model - 0.6) < 1e-9

    def test_unparseable_commence_time_treated_as_stale(self):
        """Garbage timestamp = unknown game state = fail safe, don't trade."""
        games = [_game(commence="not-a-date")]
        model = SportsOddsModel(_FakeOddsClient(games))
        result = model.estimate(
            market_id="M6", title="yes Cleveland", current_price=50.0, engine=None,
        )
        assert result is None


class TestParsingStillWorks:
    def test_prop_legs_still_parsed_for_classification(self):
        legs = parse_legs("yes Cleveland,yes Donovan Mitchell: 30+")
        assert len(legs) == 2
        assert legs[0].leg_type == "moneyline"
        assert legs[1].leg_type == "player_prop"
