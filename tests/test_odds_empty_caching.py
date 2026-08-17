"""An empty odds response is an answer. It has to be cached like one.

Measured 2026-08-17: the monthly quota went from 60 used to 500 used —
exhausted — in a matter of hours, and SportsOddsModel went dark. The DB cache
built to prevent exactly this held three rows.

The bug is one clause. `_fetch` writes the cache only `if games`, so a
successful fetch that legitimately returns no games is never recorded. In
August, two of the three configured leagues are out of season and the provider
answers 422 / empty for both. Every cycle the cache missed, every cycle charged
quota, and the five-minute cron ran that loop 288 times a day.

"No games today" is not a failure to be retried. It is the most stable fact the
API returns, and it is now cached — with a LONGER life than a live slate,
because an out-of-season league does not come back within four hours.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.modeling.odds_api import _MODULE_CACHE, EMPTY_PAYLOAD_TTL_SECONDS, OddsClient
from src.modeling.odds_store import QuotaLedger, month_key
from src.models.odds import OddsCacheEntry

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Http:
    """Counts requests so quota burn is observable, not inferred."""

    def __init__(self, payload_by_sport):
        self.payloads = payload_by_sport
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        sport = url.rstrip("/").split("/sports/")[-1].split("/")[0]
        self.calls.append(sport)
        return _Response(self.payloads.get(sport, []))


def _client(engine, http, sports, now=NOW):
    """A client as a fresh cron process sees it.

    The in-process module cache is cleared each time on purpose: the pipeline
    runs one cycle per process, so that cache never survives a cycle in
    production and a test that relied on it would prove nothing about the DB
    cache this is all about.
    """
    _MODULE_CACHE.clear()
    return OddsClient(
        api_key="k",
        sport_keys=sports,
        http=http,
        engine=engine,
        monthly_cap=500,
        enable_espn=False,
        now=lambda: now,
    )


class TestEmptyResponsesAreCached:
    def test_an_empty_slate_is_written_to_the_cache(self, engine):
        http = _Http({"basketball_nba": []})
        _client(engine, http, ["basketball_nba"]).get_all_odds()

        with get_session(engine) as session:
            rows = session.query(OddsCacheEntry).filter_by(
                sport_key="basketball_nba"
            ).count()

        assert rows == 1

    def test_the_second_cycle_spends_no_quota(self, engine):
        """The regression, stated directly: this loop burned ~440 requests."""
        http = _Http({"basketball_nba": []})

        _client(engine, http, ["basketball_nba"]).get_all_odds()
        first = len(http.calls)
        # A separate client, as a separate cron process would be.
        _client(engine, http, ["basketball_nba"]).get_all_odds()

        assert first == 1
        assert len(http.calls) == 1, "second cycle re-requested an empty slate"

    def test_quota_is_charged_once_not_twice(self, engine):
        http = _Http({"basketball_nba": []})
        ledger = QuotaLedger(engine, cap=500)

        _client(engine, http, ["basketball_nba"]).get_all_odds()
        _client(engine, http, ["basketball_nba"]).get_all_odds()

        assert ledger.used(month_key(NOW), "the_odds_api") == 1

    def test_an_out_of_season_league_gets_the_longer_ttl(self, engine):
        """An empty slate is the most stable fact the API returns. Refreshing
        it on the live-slate cadence is what spent the month."""
        http = _Http({"basketball_nba": []})
        _client(engine, http, ["basketball_nba"]).get_all_odds()

        # Past the normal TTL but inside the empty-payload TTL.
        later = NOW + dt.timedelta(seconds=EMPTY_PAYLOAD_TTL_SECONDS - 60)
        _client(engine, http, ["basketball_nba"], now=later).get_all_odds()

        assert len(http.calls) == 1

    def test_the_empty_cache_does_eventually_expire(self, engine):
        """A season starts. It must not stay dark forever."""
        http = _Http({"basketball_nba": []})
        _client(engine, http, ["basketball_nba"]).get_all_odds()

        later = NOW + dt.timedelta(seconds=EMPTY_PAYLOAD_TTL_SECONDS + 60)
        _client(engine, http, ["basketball_nba"], now=later).get_all_odds()

        assert len(http.calls) == 2


class TestLiveSlatesAreUnaffected:
    def test_a_non_empty_response_still_caches_and_serves(self, engine):
        game = {
            "event_id": "e1", "sport_key": "baseball_mlb",
            "commence_time": (NOW + dt.timedelta(hours=3)).isoformat(),
            "home_team": "A", "away_team": "B",
            "home_price": -110, "away_price": -110, "book_count": 3,
        }
        http = _Http({"baseball_mlb": [_raw_event(game)]})

        first = _client(engine, http, ["baseball_mlb"]).get_all_odds()
        _client(engine, http, ["baseball_mlb"]).get_all_odds()

        assert first, "a live slate should produce games"
        assert len(http.calls) == 1

    def test_a_failing_source_is_not_cached_as_empty(self, engine):
        """The distinction that matters: an exception is not an answer, and
        caching it as 'no games' would blind the model for a full TTL."""
        class _Boom:
            calls = []

            def get(self, url, params=None, timeout=None, **kwargs):
                _Boom.calls.append(url)
                raise RuntimeError("network down")

        _client(engine, _Boom(), ["baseball_mlb"]).get_all_odds()

        with get_session(engine) as session:
            assert session.query(OddsCacheEntry).count() == 0


def _raw_event(game):
    """The provider's shape, so the real parser runs."""
    return {
        "id": game["event_id"],
        "sport_key": game["sport_key"],
        "commence_time": game["commence_time"],
        "home_team": game["home_team"],
        "away_team": game["away_team"],
        "bookmakers": [
            {
                "key": f"book{i}",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": game["home_team"], "price": game["home_price"]},
                        {"name": game["away_team"], "price": game["away_price"]},
                    ],
                }],
            }
            for i in range(3)
        ],
    }


class TestProviderQuotaIsAuthoritative:
    """Our count is an inference. Theirs gates the account."""

    class _HttpWithHeaders(_Http):
        def __init__(self, payload_by_sport, headers):
            super().__init__(payload_by_sport)
            self._headers = headers

        def get(self, url, params=None, timeout=None, **kwargs):
            resp = super().get(url, params=params, timeout=timeout, **kwargs)
            resp.headers = self._headers
            return resp

    def test_the_ledger_adopts_the_providers_number(self, engine):
        http = self._HttpWithHeaders(
            {"baseball_mlb": []},
            {"x-requests-used": "417", "x-requests-remaining": "83"},
        )

        _client(engine, http, ["baseball_mlb"]).get_all_odds()

        assert QuotaLedger(engine, cap=500).used(month_key(NOW), "the_odds_api") == 417

    def test_a_provider_count_lower_than_ours_also_wins(self, engine):
        """Both directions. An over-count of ours blinds the model for a month;
        an under-count walks it into a 429."""
        ledger = QuotaLedger(engine, cap=500)
        ledger.charge(month_key(NOW), "the_odds_api", 300)

        http = self._HttpWithHeaders(
            {"baseball_mlb": []},
            {"x-requests-used": "12", "x-requests-remaining": "488"},
        )
        _client(engine, http, ["baseball_mlb"]).get_all_odds()

        assert ledger.used(month_key(NOW), "the_odds_api") == 12

    def test_missing_headers_leave_our_count_alone(self, engine):
        """Not every source publishes them; absence is not zero."""
        http = _Http({"baseball_mlb": []})

        _client(engine, http, ["baseball_mlb"]).get_all_odds()

        assert QuotaLedger(engine, cap=500).used(month_key(NOW), "the_odds_api") == 1
