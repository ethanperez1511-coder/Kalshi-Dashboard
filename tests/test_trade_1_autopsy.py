"""Autopsy of paper trade 1/50, and the three bugs it exposed.

Stored row, from the Neon console:

    KXHIGHCHI-26AUG13-T76 | WeatherModel | NO | qty 3 | price 92
    p_model 0.0571 | edge -0.0329 | is_paper=t

Solving backwards from that row gives last_price=9, yes_bid=8, yes_ask=9, and
the WeatherModel confidence cap of 0.85. Every number below is reproduced by
running the real gate code on those inputs, not by re-deriving the arithmetic.

The reason this file exists rather than a paragraph in a report: the three
findings are each a case where two quantities that should have been the same
were computed in two places and quietly disagreed.
"""
from __future__ import annotations

import random

import pytest

from src.ev.calculator import calculate_ev
from src.ev.filter import TradeFilter

# Reconstructed inputs.
P_MODEL = 0.0571
LAST_PRICE, YES_BID, YES_ASK = 9, 8, 9
CONFIDENCE = 0.85          # WeatherModel: max(0.5, min(0.85, 0.55 + 0.5*skill))


def _ev(**kw):
    params = dict(p_model=P_MODEL, price_cents=LAST_PRICE,
                  yes_bid=YES_BID, yes_ask=YES_ASK)
    params.update(kw)
    return calculate_ev(**params)


# --------------------------------------------------------------------------
# F1. The NO-side expected value was computed with win and loss swapped.
# --------------------------------------------------------------------------

class TestNoSideExpectedValue:
    """`raw_ev_no = (1-p)*price_no - p*(1-price_no)` reduces to `price_no - p`,
    which is not an expected value. It grew with how expensive NO was, so it
    manufactured enormous fake EV on exactly the cheap-YES longshot fades this
    system trades."""

    @pytest.mark.parametrize("p,yes_price,expected", [
        (0.10, 10, 0.0),      # correctly priced — EV must be zero
        (0.50, 50, 0.0),
        (0.90, 90, 0.0),
        (0.30, 20, -0.10),    # NO at 80c when it is worth 70c
        (0.20, 30, +0.10),    # NO at 70c when it is worth 80c
    ])
    def test_no_ev_matches_the_closed_form(self, p, yes_price, expected):
        ev = calculate_ev(p_model=p, price_cents=yes_price, fee_rate=0.0)
        assert ev.no_ev == pytest.approx(expected, abs=1e-9), (
            "the old formula reported +0.50 on the -0.10 case"
        )

    def test_no_ev_matches_simulation(self):
        """Settle it by playing the bet, not by reading the algebra."""
        rng = random.Random(11)
        p, yes_price, trials = 0.30, 20, 200_000
        price_no = (100 - yes_price) / 100.0

        wins = sum(1 for _ in range(trials) if rng.random() >= p)
        simulated = (wins * (1 - price_no) - (trials - wins) * price_no) / trials

        ev = calculate_ev(p_model=p, price_cents=yes_price, fee_rate=0.0)
        assert ev.no_ev == pytest.approx(simulated, abs=0.005)

    def test_zero_fee_ev_equals_edge_on_both_sides(self):
        """The identity that makes the two sides symmetric. The YES side always
        satisfied it; the NO side did not, which is the whole bug in one line."""
        for p, price in [(0.0571, 9), (0.30, 20), (0.62, 55), (0.9, 91)]:
            ev = calculate_ev(p_model=p, price_cents=price, fee_rate=0.0)
            assert ev.raw_ev == pytest.approx(ev.edge, abs=1e-9)
            assert ev.no_ev == pytest.approx(ev.no_edge, abs=1e-9)

    def test_an_expensive_no_is_no_longer_a_free_lunch(self):
        """A market priced exactly at the model must have zero EV on both
        sides. Under the old formula a 9c YES gave NO an EV of +0.83."""
        ev = calculate_ev(p_model=0.09, price_cents=9, fee_rate=0.0)
        assert ev.no_ev == pytest.approx(0.0, abs=1e-9)
        assert abs(ev.best_ev) < 0.01


# --------------------------------------------------------------------------
# F2. The EV was evaluated at one price and the trade filled at another.
# --------------------------------------------------------------------------

class TestEvaluationAndFillAgree:
    def test_the_no_side_is_priced_off_the_bid_not_the_last_trade(self):
        """Buying NO means selling YES into the bid, so it costs 100 - bid.
        `ORDER_TYPE` defaults to "maker", and the old calculator ignored the
        book entirely in that mode — pricing NO at 100 - last_price."""
        ev = _ev()
        assert ev.no_fill_cents == 100 - YES_BID == 92
        assert ev.no_fill_cents != 100 - LAST_PRICE, (
            "91c was the price that justified the trade; 92c was the price paid"
        )

    def test_the_one_cent_gap_was_decision_relevant(self):
        """Not a rounding detail. The threshold sat between the two prices."""
        threshold = TradeFilter()._get_edge_threshold(CONFIDENCE)
        edge_at_evaluated = (1 - P_MODEL) - (100 - LAST_PRICE) / 100.0
        edge_at_fill = (1 - P_MODEL) - (100 - YES_BID) / 100.0

        assert edge_at_evaluated >= threshold      # +0.0329 — passed
        assert edge_at_fill < threshold            # +0.0229 — should not have

    def test_the_engine_fills_where_the_calculator_prices(self):
        from src.risk.manager import TradeDecision
        from src.trading.engine import TradeEngine

        ev = _ev()
        engine = TradeEngine.__new__(TradeEngine)   # no DB needed for pricing
        decision = TradeDecision(
            approved=True, side="no", position_size_dollars=3.0,
            quantity=3, price_cents=100 - LAST_PRICE, rejection_reasons=[],
        )
        fill = engine._compute_fill_price(decision, YES_BID, YES_ASK, is_paper=True)
        assert fill == ev.no_fill_cents, (
            "two implementations of 'what does this cost' is how they diverged"
        )

    def test_a_divergence_refuses_the_trade_rather_than_reconciling_it(self, db_engine):
        """If they ever disagree again, the trade does not happen. A trade
        justified at a price it cannot get is a trade justified by nothing."""
        from src.database import Base
        from src.risk.manager import TradeDecision
        from src.trading.engine import TradeEngine

        Base.metadata.create_all(db_engine)
        engine = TradeEngine(db_engine)
        decision = TradeDecision(
            approved=True, side="no", position_size_dollars=3.0,
            quantity=3, price_cents=91, rejection_reasons=[],
        )
        result = engine.execute(
            decision=decision, market_id="KXHIGHCHI-26AUG13-T76",
            p_model=P_MODEL, implied_prob=0.09, edge=-0.0329, net_ev=0.02,
            confidence=CONFIDENCE, reasoning="autopsy",
            yes_bid=YES_BID, yes_ask=YES_ASK,
            evaluated_price=91,       # what the gate used
        )
        assert result is None, "fills at 92c, was evaluated at 91c"

    def test_a_matching_price_still_trades(self, db_engine):
        from src.database import Base
        from src.risk.manager import TradeDecision
        from src.trading.engine import TradeEngine

        Base.metadata.create_all(db_engine)
        engine = TradeEngine(db_engine)
        decision = TradeDecision(
            approved=True, side="no", position_size_dollars=3.0,
            quantity=3, price_cents=91, rejection_reasons=[],
        )
        result = engine.execute(
            decision=decision, market_id="KXHIGHCHI-26AUG13-T76",
            p_model=P_MODEL, implied_prob=0.09, edge=-0.0329, net_ev=0.02,
            confidence=CONFIDENCE, reasoning="autopsy",
            yes_bid=YES_BID, yes_ask=YES_ASK,
            evaluated_price=92, traded_edge=0.0229,
        )
        assert result is not None
        assert result["price"] == 92


# --------------------------------------------------------------------------
# F3. The stored edge was not the edge that was gated.
# --------------------------------------------------------------------------

class TestTheAutopsyIsALookup:
    def test_stored_edge_is_the_yes_edge_even_on_a_no_trade(self):
        ev = _ev()
        assert ev.recommended_side == "no"
        assert ev.edge < 0 and ev.best_edge > 0, (
            "these are different quantities and the row stored only the first"
        )

    def test_both_edges_and_the_evaluated_price_are_persisted(self, db_engine):
        from src.database import Base, get_session
        from src.models.trade import Trade
        from src.risk.manager import TradeDecision
        from src.trading.engine import TradeEngine

        Base.metadata.create_all(db_engine)
        engine = TradeEngine(db_engine)
        ev = _ev()
        engine.execute(
            decision=TradeDecision(
                approved=True, side="no", position_size_dollars=3.0,
                quantity=3, price_cents=91, rejection_reasons=[],
            ),
            market_id="KXHIGHCHI-26AUG13-T76", p_model=P_MODEL,
            implied_prob=ev.implied_prob, edge=ev.edge, net_ev=ev.net_ev,
            confidence=CONFIDENCE, reasoning="autopsy",
            yes_bid=YES_BID, yes_ask=YES_ASK,
            traded_edge=ev.best_edge, evaluated_price=ev.best_fill_cents,
        )
        with get_session(db_engine) as s:
            row = s.query(Trade).one()
            stored = (row.edge, row.traded_edge, row.evaluated_price, row.price)
        assert stored[0] == pytest.approx(-0.0329, abs=1e-4)   # YES-side edge
        assert stored[1] == pytest.approx(+0.0229, abs=1e-4)   # what was gated
        assert stored[2] == 92 and stored[3] == 92             # and at what price


# --------------------------------------------------------------------------
# The verdict, end to end.
# --------------------------------------------------------------------------

def test_trade_1_of_50_would_not_qualify_under_the_corrected_code():
    """The gate compared +0.0329 against the 0.03 tier and passed by 0.0029.
    Priced at the fill it can actually get, the edge is +0.0229 and it fails.
    """
    ev = _ev()
    verdict = TradeFilter(max_spread_cents=15).evaluate(
        ev_result=ev, confidence=CONFIDENCE, daily_volume=500,
        bid_ask_spread_cents=YES_ASK - YES_BID, hours_to_expiry=6.0,
    )
    assert ev.best_edge == pytest.approx(0.0229, abs=1e-4)
    assert verdict.status == "rejected"
    assert any("Insufficient edge" in r for r in verdict.rejection_reasons), (
        verdict.rejection_reasons
    )


def test_the_threshold_ladder_is_what_it_is(self=None):
    """Pinned because it is not what the design was described as: 5% is the
    MIDDLE tier, and high confidence lowers the bar to 3%. Trade 1/50 passed on
    the 3% tier. If the intent is a 5% floor everywhere, this test is the thing
    that has to change and it should be a deliberate edit."""
    f = TradeFilter()
    assert f._get_edge_threshold(0.85) == 0.03
    assert f._get_edge_threshold(0.70) == 0.03
    assert f._get_edge_threshold(0.55) == 0.05
    assert f._get_edge_threshold(0.30) == 0.08
