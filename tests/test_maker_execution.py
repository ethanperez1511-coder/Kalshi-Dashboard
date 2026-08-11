"""Maker execution: fill rule, walk-up cap, fractional money, shadow reporting.

Written against the pinned principle — a check that cannot fail is not a check.
Each guard here is paired with a case that DEMONSTRATES it failing: the cap is
shown refusing an order one step from crossing it, the trade-through rule is
shown rejecting a touch, and the block-trade filter is shown rejecting a print
that would otherwise fill.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.database import Base, get_session
from src.execution.fill_sim import TapeTrade, simulate_rest, trades_through
from src.execution.shadow import format_report, report_by_category, simulate_order
from src.execution.walkup import build_plan, max_price_cents
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap
from src.models.shadow import ShadowMakerOrder


@pytest.fixture
def engine(db_engine):
    Base.metadata.create_all(db_engine)
    return db_engine


def _print(ts_ms, yes_price, count, taker="no", block=False):
    return TapeTrade(
        ts_ms=ts_ms,
        yes_price=Decimal(str(yes_price)),
        no_price=Decimal(1) - Decimal(str(yes_price)),
        count=Decimal(str(count)),
        taker_outcome_side=taker,
        is_block_trade=block,
    )


def _record_trade(engine, market, ts_ms, yes_price, count, taker="no", block=False):
    payload = {"type": "trade", "msg": {
        "market_ticker": market, "ts_ms": ts_ms,
        "yes_price_dollars": str(yes_price),
        "no_price_dollars": str(1 - float(yes_price)),
        "count_fp": str(count), "taker_outcome_side": taker,
        "is_block_trade": block,
    }}
    with get_session(engine) as s:
        s.add(OrderbookDeltaRaw(
            market_ticker=market, msg_type="trade", sid=1, seq=ts_ms,
            ts_ms=ts_ms, payload=json.dumps(payload),
        ))
        s.commit()


# --------------------------------------------------------------------------
# 1. The fill rule
# --------------------------------------------------------------------------

class TestTradeThrough:
    def test_a_print_through_our_price_fills(self):
        assert trades_through(_print(1, "0.44", 5, taker="no"), "yes", 45)

    def test_a_touch_does_not_fill(self):
        """Strictly through, never on a touch. We have no queue priority and
        cannot claim a fill a real order might not have got."""
        assert not trades_through(_print(1, "0.45", 5, taker="no"), "yes", 45)

    def test_a_print_worse_than_our_price_does_not_fill(self):
        assert not trades_through(_print(1, "0.46", 5, taker="no"), "yes", 45)

    def test_a_taker_on_our_own_side_does_not_fill(self):
        """A YES taker lifts asks; it never consumes a resting YES bid."""
        assert not trades_through(_print(1, "0.44", 5, taker="yes"), "yes", 45)

    def test_block_trades_never_fill(self):
        """Matched off-book — they never touch the public ladder. Same print
        fills without the flag, which is what makes this a real check."""
        assert trades_through(_print(1, "0.44", 5, taker="no", block=False), "yes", 45)
        assert not trades_through(_print(1, "0.44", 5, taker="no", block=True), "yes", 45)

    def test_no_side_mirrors_yes_side(self):
        assert trades_through(_print(1, "0.60", 5, taker="yes"), "no", 41)
        assert not trades_through(_print(1, "0.60", 5, taker="yes"), "no", 39)


class TestSimulateRest:
    def test_fill_is_capped_by_printed_size(self):
        """A 5-contract trade-through cannot fill a 10-contract order."""
        result = simulate_rest(
            [_print(100, "0.44", 5)], "yes", 45, Decimal("10"), 0, 1000,
        )
        assert result.filled == Decimal("5")

    def test_fills_accumulate_across_prints(self):
        result = simulate_rest(
            [_print(100, "0.44", 3), _print(200, "0.43", 4)],
            "yes", 45, Decimal("10"), 0, 1000,
        )
        assert result.filled == Decimal("7")
        assert len(result.fills) == 2

    def test_prints_outside_the_window_are_ignored(self):
        result = simulate_rest(
            [_print(5_000, "0.44", 10)], "yes", 45, Decimal("10"), 0, 1000,
        )
        assert result.filled == Decimal("0")

    def test_a_sequence_gap_makes_the_fill_unproven(self):
        """Across a gap the book is unreconstructable, so resting cannot be
        proven — and an unprovable fill must never be counted as one."""
        result = simulate_rest(
            [_print(100, "0.44", 10)], "yes", 45, Decimal("10"), 0, 1000,
            gap_present=True,
        )
        assert result.unproven
        assert result.filled == Decimal("0")


# --------------------------------------------------------------------------
# 2. Fractional contracts — 1541/2299 observed prints were non-integer
# --------------------------------------------------------------------------

class TestFractionalContracts:
    def test_a_fractional_print_fills_a_fractional_amount(self):
        result = simulate_rest(
            [_print(100, "0.44", "5.04")], "yes", 45, Decimal("10"), 0, 1000,
        )
        assert result.filled == Decimal("5.04")

    def test_a_hundredth_of_a_contract_is_not_truncated_away(self):
        result = simulate_rest(
            [_print(100, "0.44", "0.01")], "yes", 45, Decimal("10"), 0, 1000,
        )
        assert result.filled == Decimal("0.01")

    def test_very_large_fractional_size(self):
        result = simulate_rest(
            [_print(100, "0.44", "10042.69")], "yes", 45, Decimal("20000"), 0, 1000,
        )
        assert result.filled == Decimal("10042.69")

    def test_many_partials_do_not_drift(self):
        """Binary floating point would leave 0.1 * 3 != 0.3 and a residual that
        fails an exposure check once a quarter, untraceably."""
        tape = [_print(100 + i, "0.44", "0.1") for i in range(100)]
        result = simulate_rest(tape, "yes", 45, Decimal("10"), 0, 10_000)
        assert result.filled == Decimal("10.0")      # exact, not 9.99999...


# --------------------------------------------------------------------------
# 3. The walk-up cap — with its failure demonstrated
# --------------------------------------------------------------------------

class TestWalkUpCap:
    def test_cap_is_p_model_minus_required_edge(self):
        # 0.62 - 0.05 = 0.57 -> 57c
        assert max_price_cents(0.62, 0.05, "yes") == 57

    def test_cap_floors_rather_than_rounds(self):
        """Rounding up hands back a fraction of the very edge this protects."""
        assert max_price_cents(0.6249, 0.05, "yes") == 57

    def test_no_side_cap_is_the_complement(self):
        # NO ceiling = 1 - (0.62 + 0.05) = 0.33 -> 33c
        assert max_price_cents(0.62, 0.05, "no") == 33

    def test_walk_stops_before_crossing_the_cap(self):
        """THE demonstrated failure: one more step would cross the cap, so the
        walk stops there instead of taking it."""
        plan = build_plan(
            start_price_cents=55, p_model=0.62, required_edge=0.05, side="yes",
            step_cents=1, max_steps=5,
        )
        assert plan.cap_cents == 57
        assert [s.price_cents for s in plan.steps] == [55, 56, 57]
        assert plan.capped_early is True
        assert "would cross the cap" in plan.reason

    def test_order_is_not_placed_at_all_when_start_exceeds_the_cap(self):
        """There is no price at which this is still the approved trade."""
        plan = build_plan(
            start_price_cents=60, p_model=0.62, required_edge=0.05, side="yes",
        )
        assert plan.steps == []
        assert plan.final_price is None
        assert "already exceeds the cap" in plan.reason

    def test_cap_never_yields_a_step_above_it(self):
        """Property: across a grid, no plan may contain a price over its cap."""
        for start in range(1, 99, 7):
            for p_model in (0.2, 0.45, 0.62, 0.9):
                for edge in (0.03, 0.05, 0.10):
                    plan = build_plan(start, p_model, edge, "yes", max_steps=9)
                    assert all(s.price_cents <= plan.cap_cents for s in plan.steps)

    def test_a_generous_cap_lets_the_walk_finish(self):
        """The guard must not be vacuous — with room, all steps are taken."""
        plan = build_plan(40, p_model=0.90, required_edge=0.03, side="yes",
                          step_cents=1, max_steps=3)
        assert len(plan.steps) == 4
        assert plan.capped_early is False


# --------------------------------------------------------------------------
# 4. Shadow orders end to end
# --------------------------------------------------------------------------

class TestShadowOrder:
    def _sim(self, engine, **kw):
        params = dict(
            market_id="M", side="yes", quantity=Decimal("10"),
            start_price_cents=45, taker_price_cents=48,
            p_model=0.62, required_edge=0.05, rest_start_ms=1_000,
            rest_seconds=30.0, category="Sports", model_name="X",
        )
        params.update(kw)
        return simulate_order(engine, **params)

    def test_a_trade_through_fills_and_records_capture(self, engine):
        _record_trade(engine, "M", 5_000, "0.44", 10)
        outcome = self._sim(engine)
        assert outcome.status == "filled"
        assert outcome.filled == Decimal("10")
        # Paid 45c where crossing would have paid 48c.
        assert outcome.capture_cents == Decimal("3")

    def test_no_trade_through_records_an_unfilled_order(self, engine):
        _record_trade(engine, "M", 5_000, "0.46", 10)      # worse than our price
        outcome = self._sim(engine)
        assert outcome.status == "unfilled"
        assert outcome.capture_cents is None

    def test_a_gap_marks_the_order_unproven_not_unfilled(self, engine):
        """Unproven and unfilled are different facts and must not be merged."""
        import datetime as dt

        _record_trade(engine, "M", 5_000, "0.44", 10)
        with get_session(engine) as s:
            s.add(OrderbookGap(
                market_ticker="M", sid=1, expected_seq=2, received_seq=9, missing=7,
                detected_at=dt.datetime.fromtimestamp(5, dt.timezone.utc),
            ))
            s.commit()
        assert self._sim(engine).status == "unproven"

    def test_partial_fill_is_recorded_as_partial(self, engine):
        _record_trade(engine, "M", 5_000, "0.44", 4)
        outcome = self._sim(engine)
        assert outcome.status == "partial"
        assert outcome.filled == Decimal("4")

    def test_shadow_never_writes_to_the_trades_table(self, engine):
        """The 50-trade gate keeps accruing on the real taker path alone."""
        from src.models.trade import Trade

        _record_trade(engine, "M", 5_000, "0.44", 10)
        self._sim(engine)
        with get_session(engine) as s:
            assert s.query(Trade).count() == 0
            assert s.query(ShadowMakerOrder).count() == 1

    def test_an_uncrossable_cap_records_not_placed(self, engine):
        outcome = self._sim(engine, start_price_cents=60)
        assert outcome.status == "not_placed"
        with get_session(engine) as s:
            assert s.query(ShadowMakerOrder).one().status == "not_placed"


# --------------------------------------------------------------------------
# 5. Reporting keeps the two biases apart
# --------------------------------------------------------------------------

class TestShadowReporting:
    def _order(self, engine, category, status, capture=None, filled="0"):
        with get_session(engine) as s:
            s.add(ShadowMakerOrder(
                market_id="M", category=category, side="yes",
                intended_quantity=Decimal("10"), filled_quantity=Decimal(filled),
                start_price_cents=45, cap_cents=57, taker_price_cents=48,
                capture_cents=Decimal(str(capture)) if capture is not None else None,
                status=status,
            ))
            s.commit()

    def test_categories_are_reported_separately(self, engine):
        """A liquid category must not carry an illiquid one through validation."""
        self._order(engine, "Sports", "filled", capture=3, filled="10")
        self._order(engine, "Climate and Weather", "unfilled")

        by_category = {r.category: r for r in report_by_category(engine)}
        assert set(by_category) == {"Sports", "Climate and Weather"}
        assert by_category["Sports"].recognised_fills == 1
        assert by_category["Climate and Weather"].recognised_fills == 0

    def test_frequency_and_capture_are_separate_fields(self, engine):
        self._order(engine, "Sports", "filled", capture=3, filled="10")
        self._order(engine, "Sports", "unfilled")

        report = report_by_category(engine)[0]
        assert report.mean_capture_cents == pytest.approx(3.0)
        assert report.fill_frequency == pytest.approx(0.5)

    def test_unproven_orders_are_excluded_from_frequency(self, engine):
        """An order we could not prove is not evidence either way."""
        self._order(engine, "Sports", "filled", capture=3, filled="10")
        self._order(engine, "Sports", "unproven")
        assert report_by_category(engine)[0].fill_frequency == pytest.approx(1.0)

    def test_output_labels_both_numbers_as_floors_and_warns_against_pooling(self, engine):
        self._order(engine, "Sports", "filled", capture=3, filled="10")
        text = format_report(report_by_category(engine))
        assert "FLOORS" in text
        assert "Do not multiply them" in text
        # No single combined PnL figure anywhere in the output.
        assert "total pnl" not in text.lower()
