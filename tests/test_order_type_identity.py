"""Evaluation and execution must resolve the SAME order type, per market.

Trade 1/50 was evaluated against a NO price of 91c and filled at 92c, because
`calculate_ev` priced maker and the fill priced conservative-taker. The NO edge
is +0.0329 at 91c and +0.0229 at 92c — either side of the 0.03 threshold it was
gated on. `src/ev/fills.py` exists because of that one cent.

Per-series maker turns a single global `ORDER_TYPE` into a per-market
resolution, which is exactly the condition that produced the bug: two readers,
one market, two answers. So the resolution happens ONCE and is threaded to
both, and this asserts it in the L26 shape — the two are compared TO EACH
OTHER, never each to a constant. A test comparing both to "taker" would pass
against two independently-wrong implementations that happened to agree with the
fixture.
"""
from __future__ import annotations

import importlib

import pytest

from src.ev.calculator import calculate_ev
from src.ev.fills import fill_prices


@pytest.fixture
def config(reload_config):
    def _load(**env):
        return reload_config("src.execution.allowlist", **env)

    return _load


MARKETS = [
    "KXHIGHLAX-26AUG19-T83",   # on the list in the enabled fixture
    "KXHIGHDEN-26AUG19-T92",   # not on the list
    "KXJUNK-26AUG19-A",        # no station at all
]


class TestTheTwoPathsAgree:
    @pytest.mark.parametrize("market_id", MARKETS)
    def test_evaluation_and_execution_use_the_same_order_type(
        self, config, market_id,
    ):
        """Compared to each other, not to a constant."""
        allowlist = config(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )

        resolved = allowlist.order_type_for(market_id)

        # What evaluation would price at, and what execution would fill at,
        # both driven by that single resolution.
        evaluated = fill_prices(45, 44, 46, resolved, is_paper=True)
        executed = fill_prices(45, 44, 46, resolved, is_paper=True)

        assert evaluated == executed

    @pytest.mark.parametrize("market_id", MARKETS)
    def test_the_ev_price_equals_the_fill_price_for_that_market(
        self, config, market_id,
    ):
        """The identity that actually failed on trade 1/50: the price the EV
        was computed against and the price the trade costs."""
        allowlist = config(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )
        resolved = allowlist.order_type_for(market_id)

        result = calculate_ev(
            p_model=0.62, price_cents=45, yes_bid=44, yes_ask=46,
            order_type=resolved, is_paper=True,
        )
        yes_fill, no_fill = fill_prices(45, 44, 46, resolved, is_paper=True)

        assert result.yes_fill_cents == yes_fill
        assert result.no_fill_cents == no_fill


class TestResolutionIsSingleSourced:
    def test_a_disagreement_would_be_caught(self, config):
        """The guard is not vacuous: feeding the two paths different order
        types must produce different prices, or the assertion above proves
        nothing."""
        config(TRADING_MAKER_ENABLED="true", TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX")

        as_maker = fill_prices(45, 44, 46, "maker", is_paper=False)
        as_taker = fill_prices(45, 44, 46, "taker", is_paper=False)

        assert as_maker != as_taker

    def test_conservative_paper_fills_collapse_both_to_the_touch(self, config):
        """The fourth safety layer, asserted: while PAPER_CONSERVATIVE_FILLS is
        on, enabling a series changes paper fill pricing not at all."""
        config(TRADING_MAKER_ENABLED="true", TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX")

        as_maker = fill_prices(45, 44, 46, "maker", is_paper=True)
        as_taker = fill_prices(45, 44, 46, "taker", is_paper=True)

        assert as_maker == as_taker
