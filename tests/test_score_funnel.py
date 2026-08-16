"""The funnel, and the model that was never dispatched.

Two production cycles reported a healthy-looking scored count while the
scorable set was starved, for two unrelated reasons. The count alone cannot
distinguish "nine markets are genuinely scorable" from "one gate ate the
universe", so every open market is now attributed to the gate that dropped it
and the attribution is required to be exhaustive.

The bug the funnel found on its first run is pinned here too: all 84 daily
temperature contracts come back from Kalshi with category "General", so a model
routed by category string had never been dispatched to a single contract it
could price — while the digest reported 21/21 cells promoted the whole time.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event

from src.database import Base, get_session
from src.ev.funnel import ScoreFunnel
from src.ev.scorer import score_all_markets
from src.models.market import Market, TERMS_PARSED
from src.models.price import PriceSnapshot


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _market(engine, market_id, category="General", *, snapshot=True,
            last_price=45, volume=500, bid=44, ask=46, age_minutes=0,
            status="open", **market_kw):
    with get_session(engine) as s:
        s.add(Market(
            market_id=market_id, title=market_kw.pop("title", f"Title for {market_id}"),
            category=category,
            close_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5),
            status=status, **market_kw,
        ))
        if snapshot:
            s.add(PriceSnapshot(
                market_id=market_id, yes_bid=bid, yes_ask=ask,
                last_price=last_price, volume=volume,
                timestamp=dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(minutes=age_minutes),
            ))
        s.commit()


# --------------------------------------------------------------------------
# 1. The partition. Every open market lands in exactly one bucket.
# --------------------------------------------------------------------------

class TestFunnelIsAPartition:
    def test_a_mixed_universe_balances(self, engine):
        _market(engine, "SCORE-1")
        _market(engine, "NOSNAP-1", snapshot=False)
        _market(engine, "STALE-1", age_minutes=999)
        _market(engine, "NOPRICE-1", last_price=0)
        _market(engine, "CLOSED-1", status="closed")

        f = ScoreFunnel()
        score_all_markets(engine, funnel=f)

        assert f.open_markets == 4, "closed markets are not part of the universe"
        assert f.balances(), f.format()
        assert f.stale_or_missing_snapshot == 2   # never written + aged out
        assert f.no_last_price == 1

    def test_an_uncounted_rejection_path_is_reported_not_hidden(self):
        """The check that makes the rest of the file trustworthy.

        Every counter in this module is incremented next to a `continue`. The
        failure mode is a future `continue` that nobody counts, which would make
        the funnel quietly under-report rather than error. The imbalance is
        surfaced instead."""
        f = ScoreFunnel(open_markets=100, with_fresh_snapshot=100, scored=1)
        assert f.balances() is False
        assert "UNATTRIBUTED" in f.format()
        assert "99" in f.format()

    def test_a_balanced_funnel_does_not_cry_wolf(self):
        f = ScoreFunnel(open_markets=10, with_fresh_snapshot=10,
                        no_last_price=4, no_model_estimate=5, scored=1)
        assert f.balances() is True
        assert "UNATTRIBUTED" not in f.format()

    def test_budget_truncation_is_attributed_not_absorbed(self, engine):
        """A stage that stopped early and a universe that had nothing to score
        produce the same scored count. They are not the same situation."""
        from src.deadline import Deadline

        for i in range(10):
            _market(engine, f"M-{i}")

        expired = Deadline(0.0, "scoring")
        f = ScoreFunnel()
        score_all_markets(engine, deadline=expired, funnel=f)

        assert f.budget_skipped == 10
        assert f.scored == 0
        assert f.balances(), f.format()


# --------------------------------------------------------------------------
# 2. Dispatch scope. The bug the funnel found.
# --------------------------------------------------------------------------

class TestModelDispatchScope:
    """A model routed by category is retired the moment the exchange relabels a
    market, and nothing reports the retirement."""

    def test_weather_claims_a_temperature_contract_labelled_general(self):
        """Measured live 2026-08-13: every one of the 84 daily temperature
        contracts is returned with category "General"."""
        from src.weather.model import WeatherModel

        model = WeatherModel()
        assert model.matches("General") is False, (
            "the category test is genuinely false here — which is why routing "
            "on it silenced the model"
        )
        assert model.claims("KXHIGHNY-26AUG13-T92", "General") is True

    def test_weather_does_not_claim_an_unrelated_market(self):
        from src.weather.model import WeatherModel

        model = WeatherModel()
        assert model.claims("KXNEXTISRAELPM-45JAN01-GEIS", "Politics") is False

    def test_weather_still_claims_its_own_category(self):
        from src.weather.model import WeatherModel

        assert WeatherModel().claims("SOMETHING-ELSE", "Climate and Weather") is True

    def test_registry_dispatches_weather_to_a_general_temperature_contract(self):
        from src.modeling.registry import ModelRegistry
        from src.weather.model import WeatherModel

        models = ModelRegistry().get_models_for("General", "KXHIGHNY-26AUG13-T92")
        assert any(isinstance(m, WeatherModel) for m in models), (
            "the whole weather model was unreachable in production through "
            "exactly this call"
        )

    def test_registry_does_not_dispatch_weather_to_other_general_markets(self):
        from src.modeling.registry import ModelRegistry
        from src.weather.model import WeatherModel

        models = ModelRegistry().get_models_for("General", "KXSOMETHING-ELSE")
        assert not any(isinstance(m, WeatherModel) for m in models)

    def test_models_without_an_override_route_by_category_exactly_as_before(self):
        """`claims` defaults to `matches`, so nothing else changed."""
        from src.modeling.models.finance import FinanceModel

        model = FinanceModel()
        assert model.claims("ANY-TICKER", "Economics") == model.matches("Economics")
        assert model.claims("ANY-TICKER", "Sports") == model.matches("Sports")


# --------------------------------------------------------------------------
# 3. Per-model refusal reasons
# --------------------------------------------------------------------------

class TestRefusalReasons:
    def test_weather_names_the_gate_that_fired(self, engine):
        """Four independent gates can each produce silence. "0 of 28 priced" is
        the same number for a stale fit and a tripped tripwire."""
        from src.weather.model import WeatherModel

        # Parsed terms, station mapped, but the target date is in the past.
        _market(engine, "KXHIGHNY-20AUG12-T90", category="General",
                series_ticker="KXHIGHNY", strike_direction="above",
                strike_value=90.0, strike_unit="F", terms_status=TERMS_PARSED)
        model = WeatherModel(now=lambda: dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc))
        assert model.estimate("KXHIGHNY-20AUG12-T90", "t", 0.42, engine) is None
        assert model.refusals["lead_past"] == 1

        assert model.estimate("KXHIGHNY-26AUG14-T90", "t", 0.42, engine) is None
        assert model.refusals["terms_not_parsed"] == 1

        assert model.estimate("KXNOTAWEATHERMARKET", "t", 0.42, engine) is None
        assert model.refusals["no_station_mapping"] == 1

    def test_the_funnel_collects_them(self, engine):
        _market(engine, "KXHIGHNY-20AUG12-T90", category="General",
                series_ticker="KXHIGHNY", strike_direction="above",
                strike_value=90.0, strike_unit="F", terms_status=TERMS_PARSED)
        f = ScoreFunnel()
        score_all_markets(engine, funnel=f)
        assert f.model_refusals.get("WeatherModel"), f.format()
        assert "WeatherModel refusals" in f.format()

    def test_odds_feed_state_distinguishes_a_dark_source_from_a_quiet_market(self, engine):
        """Zero sports markets scoring in season is a red flag for the source,
        not the market — but only if the source state is recorded."""
        _market(engine, "M-1", category="Sports")
        f = ScoreFunnel()
        score_all_markets(engine, funnel=f)
        assert "key_set" in f.odds_state
        assert "odds feed:" in f.format()


# --------------------------------------------------------------------------
# 4. The coupling that would silently empty the funnel
# --------------------------------------------------------------------------

def test_snapshot_heartbeat_must_stay_inside_the_staleness_cutoff():
    """Two independently-configurable constants, one hidden dependency.

    Unchanged quotes are only rewritten every SNAPSHOT_HEARTBEAT_MINUTES, and
    the scorer discards any snapshot older than MAX_SNAPSHOT_AGE_MINUTES. If
    the heartbeat is ever raised past the cutoff — an obvious move for keeping
    the database small — every market whose price has not moved becomes
    permanently unscorable, and the only symptom is a smaller scored count.
    """
    from src.trading_config import (
        MAX_SNAPSHOT_AGE_MINUTES,
        SNAPSHOT_HEARTBEAT_MINUTES,
    )

    assert SNAPSHOT_HEARTBEAT_MINUTES < MAX_SNAPSHOT_AGE_MINUTES, (
        f"heartbeat {SNAPSHOT_HEARTBEAT_MINUTES}min >= staleness cutoff "
        f"{MAX_SNAPSHOT_AGE_MINUTES}min: a market with a steady price would "
        "age out of the scorer before its snapshot is refreshed"
    )


# --------------------------------------------------------------------------
# 5. What the recorder actually needs
# --------------------------------------------------------------------------

def _opportunity(market_id, status, net_ev):
    from src.models.opportunity import Opportunity

    return Opportunity(
        market_id=market_id, status=status, net_ev=net_ev,
        p_model=0.5, implied_prob=0.45, edge=0.05, recommended_side="YES",
        confidence=0.6, reasoning="test", model_name="TestModel",
    )

class TestRecorderSubscribeList:
    def test_scored_but_rejected_markets_are_not_recorded(self, engine):
        """"9 scored" was never the recorder's precondition. It subscribes to
        qualifying/watching opportunities, so a cycle can score markets and
        still hand it nothing."""
        from src.models.opportunity import Opportunity
        from src.recorder.book_recorder import markets_to_record

        with get_session(engine) as s:
            for i in range(9):
                s.add(_opportunity(f"M-{i}", "rejected", -0.01))
            s.commit()

        assert markets_to_record(engine) == []

    def test_a_watching_opportunity_is_enough(self, engine):
        from src.models.opportunity import Opportunity
        from src.recorder.book_recorder import markets_to_record

        from src.models.market import Market

        with get_session(engine) as s:
            # A live market row: the subscribe list refuses any candidate whose
            # liveness it cannot establish, so production shape is required.
            s.add(Market(
                market_id="M-1", title="t", category="Weather",
                close_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
                status="active",
            ))
            s.add(_opportunity("M-0", "rejected", -0.01))
            s.add(_opportunity("M-1", "watching", 0.02))
            s.commit()

        assert markets_to_record(engine) == ["M-1"]
