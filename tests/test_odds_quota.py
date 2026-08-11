"""Odds API quota survival (Phase 1.2, 2026-08-11).

The bug: the odds cache lived in a module-level dict.

    _MODULE_CACHE: Dict[str, tuple] = {}

That works for a long-lived process. The bot now runs as a GitHub Actions cron
(`*/5 * * * *`), so every cycle is a **fresh Python process** and the cache is
always empty — the 60-minute TTL never applied in production. Burn was
288 runs/day x 3 sports = ~864 requests/day against a ~500/month free tier, so
the quota died inside a day and `SportsOddsModel` then returned None all month.

Fixes under test:
  1. the cache is persisted in the DB, so it survives the process;
  2. a per-month quota ledger hard-stops spending before the API refuses;
  3. gates that need no model (volume/spread/expiry) can run *before* model
     dispatch, so quota is never spent on a market that cannot qualify;
  4. a free second source (ESPN) can serve a sport when the paid one is dark.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.database import Base, get_session
from src.ev.filter import TradeFilter
from src.ev.calculator import calculate_ev
from src.modeling.odds_api import GameOdds, OddsClient, _clear_module_cache
from src.modeling.odds_sources import EspnOddsSource, TheOddsApiSource
from src.modeling.odds_store import OddsCacheStore, QuotaLedger, quota_snapshot
from src.models.odds import OddsCacheEntry, OddsQuotaUsage


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_module_cache():
    _clear_module_cache()
    yield
    _clear_module_cache()


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


# --------------------------------------------------------------------------
# fixtures: fake HTTP
# --------------------------------------------------------------------------

def _odds_api_payload(home="Detroit Tigers", away="Cleveland Guardians"):
    return [{
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-08-11T23:10:00Z",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": -123},
                    {"name": away, "price": 114},
                ],
            }],
        }],
    }]


def _espn_payload(with_odds=True, final=False):
    comp = {
        "competitors": [
            {"homeAway": "home", "team": {"displayName": "Detroit Tigers"}},
            {"homeAway": "away", "team": {"displayName": "Cleveland Guardians"}},
        ],
        "date": "2026-08-11T23:10Z",
        "status": {"type": {"state": "post" if final else "pre"}},
    }
    if with_odds:
        comp["odds"] = [{
            "provider": {"id": "100", "name": "DraftKings"},
            "overUnder": 8.5,
            "spread": -1.5,
            "moneyline": {
                "home": {"close": {"odds": "-123"}, "open": {"odds": "-129"}},
                "away": {"close": {"odds": "+114"}, "open": {"odds": "+107"}},
            },
        }]
    return {"events": [{"id": "401816481", "competitions": [comp]}]}


class _FakeHttp:
    """Stand-in for the httpx module — records every GET."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        resp = MagicMock()
        resp.status_code = self.status
        resp.json.return_value = (
            self.payload(url) if callable(self.payload) else self.payload
        )
        return resp


def _client(engine, http, **kw):
    kw.setdefault("sport_keys", ["baseball_mlb"])
    kw.setdefault("engine", engine)
    kw.setdefault("now", lambda: NOW)
    return OddsClient("test-key", http=http, **kw)


# --------------------------------------------------------------------------
# 1. The cache must survive the process
# --------------------------------------------------------------------------

class TestPersistentCache:
    def test_cold_process_serves_from_db_without_http(self, engine):
        """THE regression: a brand-new process (empty module cache) must not
        spend quota when the DB holds a fresh entry."""
        warm = _FakeHttp(_odds_api_payload())
        _client(engine, warm).get_all_odds()
        assert len(warm.calls) == 1  # first process paid once

        _clear_module_cache()  # simulate the next cron tick: fresh interpreter
        cold = _FakeHttp(_odds_api_payload())
        games = _client(engine, cold).get_all_odds()

        assert cold.calls == [], "cold process re-burned the quota"
        assert len(games) == 1
        assert games[0].home_team == "Detroit Tigers"

    def test_successful_fetch_is_persisted(self, engine):
        http = _FakeHttp(_odds_api_payload())
        _client(engine, http).get_all_odds()
        with get_session(engine) as s:
            entry = s.query(OddsCacheEntry).filter_by(sport_key="baseball_mlb").one()
            assert json.loads(entry.payload)[0]["home_team"] == "Detroit Tigers"
            assert entry.source == "the_odds_api"

    def test_expired_entry_refetches(self, engine):
        http = _FakeHttp(_odds_api_payload())
        _client(engine, http, ttl_seconds=3600).get_all_odds()
        _clear_module_cache()

        later = lambda: NOW + timedelta(hours=2)
        _client(engine, http, ttl_seconds=3600, now=later).get_all_odds()
        assert len(http.calls) == 2

    def test_only_the_expired_sport_refetches(self, engine):
        """Per-sport TTLs: a stale NBA entry must not drag MLB along with it."""
        # The store holds serialised GameOdds, not the provider's raw JSON.
        def cached(home):
            return [asdict(GameOdds(
                sport="x", home_team=home, away_team="Away",
                home_win_prob=0.55, away_win_prob=0.45, draw_prob=0.0,
                totals={}, spreads={}, commence_time="2026-08-11T23:10:00Z",
            ))]

        store = OddsCacheStore(engine)
        store.put("baseball_mlb", "the_odds_api", cached("Detroit Tigers"), now=NOW)
        store.put(
            "basketball_nba", "the_odds_api", cached("Boston"),
            now=NOW - timedelta(hours=9),
        )
        _clear_module_cache()

        http = _FakeHttp(_odds_api_payload("Boston", "Miami"))
        _client(
            engine, http, sport_keys=["baseball_mlb", "basketball_nba"],
            ttl_seconds=4 * 3600,
        ).get_all_odds()

        assert len(http.calls) == 1
        assert "basketball_nba" in http.calls[0][0]

    def test_failed_fetch_is_not_cached(self, engine):
        """Caching an empty quota-dead result would hide recovery."""
        http = _FakeHttp([], status=429)
        _client(engine, http).get_all_odds()
        with get_session(engine) as s:
            assert s.query(OddsCacheEntry).count() == 0


# --------------------------------------------------------------------------
# 2. Budget before spend
# --------------------------------------------------------------------------

class TestQuotaLedger:
    def test_fetch_charges_the_ledger(self, engine):
        http = _FakeHttp(_odds_api_payload())
        _client(engine, http).get_all_odds()
        assert QuotaLedger(engine, cap=500).used("2026-08", "the_odds_api") == 1

    def test_exhausted_quota_blocks_http(self, engine):
        ledger = QuotaLedger(engine, cap=500)
        ledger.charge("2026-08", "the_odds_api", 500)
        http = _FakeHttp(_odds_api_payload())
        client = _client(engine, http, monthly_cap=500)
        games = client.get_all_odds()

        assert http.calls == [], "spent quota we did not have"
        assert games == []
        assert client.quota_dead is True

    def test_ledger_is_per_month(self, engine):
        ledger = QuotaLedger(engine, cap=500)
        ledger.charge("2026-07", "the_odds_api", 500)
        assert ledger.used("2026-08", "the_odds_api") == 0
        assert ledger.remaining("2026-08", "the_odds_api") == 500

    def test_free_source_does_not_charge(self, engine):
        assert TheOddsApiSource("k").costs_quota is True
        assert EspnOddsSource().costs_quota is False


class TestQuotaProjection:
    def test_burn_rate_and_exhaustion_projection(self, engine):
        QuotaLedger(engine, cap=500).charge("2026-08", "the_odds_api", 100)
        # 2026-08-11T12:00 => 10 full days elapsed + half of the 11th = 10.5
        snap = quota_snapshot(engine, cap=500, now=NOW)

        assert snap["used"] == 100
        assert snap["cap"] == 500
        assert snap["remaining"] == 400
        assert snap["burn_per_day"] == pytest.approx(100 / 10.5, rel=1e-3)
        # 31-day month
        assert snap["projected_month_end"] == pytest.approx(100 / 10.5 * 31, rel=1e-3)
        assert snap["days_to_exhaustion"] == pytest.approx(400 / (100 / 10.5), rel=1e-3)
        # 9.5/day x 31 = 295 < 500 — this burn rate finishes the month inside cap.
        assert snap["projected_overrun"] is False

    def test_overrun_is_flagged(self, engine):
        """The old cron burn — ~864/day — must project a blowout, loudly."""
        QuotaLedger(engine, cap=500).charge("2026-08", "the_odds_api", 400)
        snap = quota_snapshot(engine, cap=500, now=NOW)
        assert snap["projected_month_end"] > 500
        assert snap["projected_overrun"] is True
        assert snap["days_to_exhaustion"] < 31

    def test_zero_burn_never_divides_by_zero(self, engine):
        snap = quota_snapshot(engine, cap=500, now=NOW)
        assert snap["used"] == 0
        assert snap["burn_per_day"] == 0.0
        assert snap["days_to_exhaustion"] is None
        assert snap["projected_overrun"] is False


# --------------------------------------------------------------------------
# 3. Gate before spend — and prove it changes no decision
# --------------------------------------------------------------------------

class TestPrescreen:
    """`prescreen` must be a strict subset of `evaluate`: anything it rejects
    would have been rejected anyway, so skipping the model is decision-neutral."""

    def test_prescreen_rejection_implies_evaluate_rejection(self):
        f = TradeFilter(
            min_daily_volume=100, max_spread_cents=3,
            min_hours_to_expiry=1.0, max_hours_to_expiry=14 * 24,
        )
        ev = calculate_ev(p_model=0.90, price_cents=50, yes_bid=49, yes_ask=51)

        checked = 0
        for volume in (0, 50, 99, 100, 5000):
            for spread in (0, 1, 3, 4, 20):
                for hours in (0.0, 0.5, 1.0, 100.0, 400.0):
                    if f.prescreen(volume, spread, hours):
                        continue
                    result = f.evaluate(
                        ev_result=ev, confidence=0.85, daily_volume=volume,
                        bid_ask_spread_cents=spread, hours_to_expiry=hours,
                    )
                    assert result.status != "qualifying", (
                        f"prescreen rejected but evaluate qualified: "
                        f"vol={volume} spread={spread} hours={hours}"
                    )
                    checked += 1
        assert checked > 0, "grid never exercised a prescreen rejection"

    def test_prescreen_passes_a_healthy_market(self):
        f = TradeFilter(min_daily_volume=100, max_spread_cents=3, min_hours_to_expiry=1.0)
        assert f.prescreen(daily_volume=5000, bid_ask_spread_cents=2, hours_to_expiry=48) is True

    def test_prescreen_ignores_model_dependent_gates(self):
        """It must only consult market facts. A market with great liquidity
        passes prescreen even if its edge would later fail — that is evaluate's
        job, and prescreen must never pre-empt it."""
        f = TradeFilter(min_daily_volume=100, max_spread_cents=3)
        assert f.prescreen(1000, 1, 24) is True


# --------------------------------------------------------------------------
# 4. ESPN as a free fallback source
# --------------------------------------------------------------------------

class TestEspnSource:
    def test_parses_moneyline_into_devigged_probabilities(self):
        http = _FakeHttp(_espn_payload())
        games = EspnOddsSource(http=http, now=lambda: NOW).fetch("baseball_mlb")

        assert len(games) == 1
        g = games[0]
        assert g.home_team == "Detroit Tigers"
        assert g.away_team == "Cleveland Guardians"
        # -123 -> 0.5516, +114 -> 0.4673, overround 1.0189 -> devig 0.5414
        assert g.home_win_prob == pytest.approx(0.5414, abs=1e-3)
        assert g.away_win_prob == pytest.approx(0.4586, abs=1e-3)
        assert g.home_win_prob + g.away_win_prob == pytest.approx(1.0)
        assert g.source == "espn"
        assert g.book_count == 1

    def test_requests_an_explicit_date(self):
        """The bare endpoint returns *yesterday's* slate — always pass ?dates."""
        http = _FakeHttp(_espn_payload())
        EspnOddsSource(http=http, now=lambda: NOW).fetch("baseball_mlb")
        assert http.calls[0][1]["dates"] == "20260811"

    def test_event_without_odds_is_skipped_not_crashed(self):
        """ESPN drops the `odds` key entirely once a game is FINAL."""
        http = _FakeHttp(_espn_payload(with_odds=False))
        assert EspnOddsSource(http=http, now=lambda: NOW).fetch("baseball_mlb") == []

    def test_unmapped_sport_returns_nothing(self):
        http = _FakeHttp(_espn_payload())
        assert EspnOddsSource(http=http, now=lambda: NOW).fetch("tennis_atp") == []
        assert http.calls == []

    def test_sets_no_custom_user_agent(self):
        """ESPN's Akamai bot manager 403s browser-like and custom UAs; the
        library default passes. Guard against a well-meaning UA being added."""
        http = _FakeHttp(_espn_payload())
        EspnOddsSource(http=http, now=lambda: NOW).fetch("baseball_mlb")
        url, params = http.calls[0]
        assert "headers" not in params


class TestSourceFallback:
    def test_espn_covers_the_sport_when_paid_quota_is_dead(self, engine):
        ledger = QuotaLedger(engine, cap=500)
        ledger.charge("2026-08", "the_odds_api", 500)

        def payload(url):
            return _espn_payload() if "espn" in url else _odds_api_payload()

        http = _FakeHttp(payload)
        client = _client(engine, http, monthly_cap=500, enable_espn=True)
        games = client.get_all_odds()

        assert len(games) == 1
        assert games[0].source == "espn"
        assert all("espn" in url for url, _ in http.calls)

    def test_espn_stays_off_by_default(self, engine):
        ledger = QuotaLedger(engine, cap=500)
        ledger.charge("2026-08", "the_odds_api", 500)
        http = _FakeHttp(_espn_payload())
        assert _client(engine, http, monthly_cap=500).get_all_odds() == []
        assert http.calls == []
