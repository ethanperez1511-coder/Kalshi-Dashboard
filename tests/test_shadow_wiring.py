"""The simulator was fully built and nothing ever called it.

`fill_sim`, `walkup`, `shadow` and `preflight` were written, unit-tested and
correct. Grepping the tree for callers of `simulate_order` returned the tests
and `preflight` (which only reads the reports back). The pipeline never invoked
it, so no shadow order had ever been recorded in production.

That is the L27 shape for the fourth time in this project — `run_pipeline`'s
unassigned clock, thirteen unpushed commits, `_retire` below the `__main__`
guard, and now an entire measurement subsystem wired to nothing. In each case
the unit was right and the path between it and production had no test.

So these tests are about the PATH. They drive the real execution loop and
assert what it did or did not call, rather than importing `simulate_order` and
proving once more that it works.

The safety property is asserted the same way, because "shadow only" is a claim
about wiring rather than about the simulator: the shadow path must never touch
`trades`, `positions`, or the 50-trade gate counter. Those carry the meaning of
the live gate and a simulation writing into them would make that gate mean two
things at once.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from src.database import Base, get_session
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.position import Position
from src.models.settings import TradingSettings
from src.models.shadow import ShadowMakerOrder
from src.models.trade import Trade

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)
MARKET = "KXHIGHNY-26AUG18-T90"


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(TradingSettings(bankroll=100.0, paper_trade_count=0, mode="paper"))
        session.add(Market(
            market_id=MARKET, title="t", category="Weather",
            close_date=NOW + dt.timedelta(days=2), status="active",
        ))
        session.commit()
    return db_engine


def _qualifying():
    """The shape `run_pipeline` hands the execution stage."""
    return [{
        "market_id": MARKET, "p_model": 0.62, "implied_prob": 0.45,
        "edge": 0.17, "net_ev": 0.05, "recommended_side": "yes",
        "confidence": 0.8, "reasoning": "t", "model_name": "WeatherModel",
        "yes_bid": 44, "yes_ask": 46, "category": "Weather",
    }]


class _Spy:
    """Records calls instead of running the simulator."""

    def __init__(self):
        self.calls = []

    def __call__(self, engine, **kwargs):
        self.calls.append(kwargs)
        from src.execution.shadow import ShadowOutcome

        return ShadowOutcome("unfilled", Decimal("0"), None, 1, None, "spy")


class _Alerter:
    def trade(self, *args, **kwargs):
        return True


def _run_execution(engine, monkeypatch, shadow_enabled, qualifying=None):
    """Drive the real execution loop, not a re-implementation of it."""
    import src.run_trading as rt

    spy = _Spy()
    monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", shadow_enabled)
    monkeypatch.setattr(rt, "simulate_shadow_order", spy)
    rt.execute_qualifying(
        engine,
        _qualifying() if qualifying is None else qualifying,
        _Alerter(),
        now=NOW,
    )
    return spy


class TestTheWiringExists:
    def test_a_placed_trade_triggers_the_simulator(self, engine, monkeypatch):
        spy = _run_execution(engine, monkeypatch, shadow_enabled=True)

        assert spy.calls, "a paper trade was placed and nothing simulated it"
        assert spy.calls[0]["market_id"] == MARKET

    def test_the_flag_default_off_means_no_call(self, engine, monkeypatch):
        """SHADOW_MAKER_ENABLED is false everywhere until evidence exists."""
        spy = _run_execution(engine, monkeypatch, shadow_enabled=False)

        assert spy.calls == []

    def test_the_simulator_receives_the_price_that_was_actually_paid(
        self, engine, monkeypatch,
    ):
        """Capture is measured against the taker price of the real fill. Passing
        the quoted price instead would compare the maker path to a trade that
        never happened."""
        spy = _run_execution(engine, monkeypatch, shadow_enabled=True)

        with get_session(engine) as session:
            trade = session.query(Trade).one()
            paid = trade.price
        assert spy.calls[0]["taker_price_cents"] == paid


class TestShadowNeverTouchesTheRealRecord:
    def test_a_simulation_does_not_move_the_gate_counter(self, engine, monkeypatch):
        """The 50-trade gate must keep exactly one meaning."""
        _run_execution(engine, monkeypatch, shadow_enabled=True)

        with get_session(engine) as session:
            settings = session.query(TradingSettings).one()
            # One taker paper trade, and only that.
            assert settings.paper_trade_count == 1
            assert session.query(Trade).count() == 1
            assert session.query(Position).count() == 1

    def test_a_failing_simulation_does_not_fail_the_cycle(self, engine, monkeypatch):
        """Shadow simulation is reporting. Reporting must never break trading."""
        import src.run_trading as rt

        def _explode(engine, **kwargs):
            raise RuntimeError("replay refused: sequence gap")

        monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", True)
        monkeypatch.setattr(rt, "simulate_shadow_order", _explode)

        rt.execute_qualifying(engine, _qualifying(), _Alerter(), now=NOW)

        with get_session(engine) as session:
            assert session.query(Trade).count() == 1

    def test_nothing_is_simulated_when_no_trade_was_placed(self, engine, monkeypatch):
        """No fill to compare against means nothing to measure."""
        spy = _run_execution(engine, monkeypatch, shadow_enabled=True, qualifying=[])

        assert spy.calls == []
        with get_session(engine) as session:
            assert session.query(ShadowMakerOrder).count() == 0


class TestShadowIsVisiblePerCycle:
    """A day of accumulation you cannot see is a day you cannot trust.

    The shadow section was only rendered inside the daily heartbeat block, so
    the first cycles after the flag was set reported nothing and there was no
    way to tell "the simulator ran and refused" from "the flag never reached
    the job". Those are completely different problems and they looked
    identical.
    """

    def test_the_execution_summary_names_each_shadow_outcome(self, engine, monkeypatch):
        import src.run_trading as rt
        from src.execution.shadow import ShadowOutcome

        def _unproven(engine, **kwargs):
            return ShadowOutcome(
                "unproven", Decimal("0"), None, 1, None, "sequence gap",
            )

        monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", True)
        monkeypatch.setattr(rt, "simulate_shadow_order", _unproven)

        funnel, _ = rt.execute_qualifying(engine, _qualifying(), _Alerter(), now=NOW)

        assert funnel.shadow_outcomes["unproven"] == 1
        assert "unproven" in funnel.format()

    def test_a_failed_simulation_is_counted_not_swallowed(self, engine, monkeypatch):
        """Exception-isolated is not the same as unreported."""
        import src.run_trading as rt

        def _explode(engine, **kwargs):
            raise RuntimeError("replay refused")

        monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", True)
        monkeypatch.setattr(rt, "simulate_shadow_order", _explode)

        funnel, _ = rt.execute_qualifying(engine, _qualifying(), _Alerter(), now=NOW)

        assert funnel.shadow_outcomes["error"] == 1

    def test_nothing_is_reported_when_the_flag_is_off(self, engine, monkeypatch):
        """Silence must mean off, and only off."""
        import src.run_trading as rt

        monkeypatch.setattr(rt, "SHADOW_MAKER_ENABLED", False)
        monkeypatch.setattr(rt, "simulate_shadow_order", _Spy())

        funnel, _ = rt.execute_qualifying(engine, _qualifying(), _Alerter(), now=NOW)

        assert not funnel.shadow_outcomes
        assert "shadow" not in funnel.format().lower()
