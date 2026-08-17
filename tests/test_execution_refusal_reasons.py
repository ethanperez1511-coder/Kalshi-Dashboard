"""An execution refusal must name itself, like every other refusal here.

The funnel reported "execution returned none 1" for the only qualifying
opportunity in a cycle and stopped there. Every other rejection path in this
system carries a reason — risk limits carry their limit message, the weather
model carries six distinct refusal counters, the scoring funnel attributes
every market to a named bucket. This one path said only that something did not
happen, which is the state the funnels were built to abolish.

`TradeEngine.execute` has exactly three paths that return None, and they mean
completely different things: a risk decision that was already rejected, a
market we already hold, and a fill price that no longer matches the price the
edge was computed at. The third is a genuine alarm — it means the market moved
under the gate — and the second is routine. Reporting them as one number makes
the alarm invisible.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.database import Base, get_session
from src.execution.funnel import ExecutionFunnel
from src.models.position import Position
from src.models.settings import TradingSettings
from src.risk.manager import TradeDecision
from src.trading.engine import TradeEngine


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(TradingSettings(bankroll=100.0, paper_trade_count=0))
        session.commit()
    return db_engine


def _decision(approved=True, quantity=3, price_cents=92, side="no"):
    return TradeDecision(
        approved=approved,
        side=side,
        quantity=quantity,
        price_cents=price_cents,
        position_size_dollars=round(quantity * price_cents / 100.0, 2),
        rejection_reasons=[] if approved else ["some limit"],
    )


def _execute(engine, **overrides):
    te = TradeEngine(engine)
    kwargs = dict(
        decision=_decision(),
        market_id="KXHIGHNY-26AUG18-T91",
        p_model=0.4, implied_prob=0.5, edge=0.1, net_ev=0.05, confidence=0.8,
        reasoning="t", yes_bid=7, yes_ask=9, model_name="WeatherModel",
    )
    kwargs.update(overrides)
    return te, te.execute(**kwargs)


class TestEveryNoneReturnNamesItself:
    def test_a_held_market_is_named(self, engine):
        with get_session(engine) as session:
            session.add(Position(
                market_id="KXHIGHNY-26AUG18-T91", side="no", entry_price=90,
                quantity=1, current_price=90, status="open",
            ))
            session.commit()

        te, result = _execute(engine)

        assert result is None
        assert te.last_refusal == "position_already_open"
        assert te.refusals["position_already_open"] == 1

    def test_a_diverged_fill_price_is_named(self, engine):
        """The alarm. The edge that passed the gate is not the edge available,
        which is how trade 1/50 was justified by a price it never got."""
        te, result = _execute(engine, evaluated_price=50)

        assert result is None
        assert te.last_refusal == "fill_price_diverged"

    def test_an_unapproved_decision_is_named(self, engine):
        te, result = _execute(engine, decision=_decision(approved=False))

        assert result is None
        assert te.last_refusal == "risk_rejected"

    def test_a_successful_trade_records_no_refusal(self, engine):
        te, result = _execute(engine)

        assert result is not None
        assert te.last_refusal is None
        assert not te.refusals

    def test_last_refusal_resets_between_calls(self, engine):
        """A stale reason attached to the next opportunity is worse than none."""
        te = TradeEngine(engine)
        common = dict(
            market_id="KXHIGHNY-26AUG18-T91", p_model=0.4, implied_prob=0.5,
            edge=0.1, net_ev=0.05, confidence=0.8, reasoning="t",
            yes_bid=7, yes_ask=9,
        )
        te.execute(decision=_decision(approved=False), **common)
        assert te.last_refusal == "risk_rejected"

        te.execute(decision=_decision(), **common)
        assert te.last_refusal is None


class TestFunnelCarriesTheReasons:
    def test_reasons_appear_in_the_formatted_block(self):
        funnel = ExecutionFunnel(qualifying=2)
        funnel.record_execution_nothing("position_already_open")
        funnel.record_execution_nothing("fill_price_diverged")

        text = funnel.format()

        assert "position_already_open: 1" in text
        assert "fill_price_diverged: 1" in text

    def test_the_count_still_balances_the_partition(self):
        funnel = ExecutionFunnel(qualifying=1)
        funnel.record_execution_nothing("position_already_open")

        assert funnel.execution_returned_nothing == 1
        assert funnel.balances()

    def test_an_unnamed_refusal_is_still_counted_and_flagged(self):
        """A new None path added later must not silently vanish."""
        funnel = ExecutionFunnel(qualifying=1)
        funnel.record_execution_nothing(None)

        assert funnel.execution_returned_nothing == 1
        assert "unspecified" in funnel.format()

    def test_the_headline_names_the_dominant_reason(self):
        funnel = ExecutionFunnel(qualifying=3, placed=0)
        funnel.record_execution_nothing("position_already_open")
        funnel.record_execution_nothing("position_already_open")
        funnel.record_execution_nothing("fill_price_diverged")

        assert "position_already_open" in funnel.headline()
