"""The one place that decides what a contract costs.

Trade 1/50 was evaluated against a NO price of 91c and filled at 92c. The
evaluation used `100 - last_price` because `ORDER_TYPE` is "maker" and
`calculate_ev` ignored the book in that mode; the fill used `100 - yes_bid`
because paper fills are deliberately conservative. Both were individually
defensible and together they were a trade that qualified only because the two
disagreed — the NO edge is +0.0329 at 91c and +0.0229 at 92c, either side of
the 0.03 threshold it was gated on.

So evaluation and execution now call the same function. A price gap between
what justified a trade and what the trade cost is not a rounding detail: it is
the difference between an edge and no edge at exactly the size of edge this
system trades on.

Last price is the fallback and never the preference. It is the price of
somebody else's trade at some earlier moment, not a price available now.
"""
from __future__ import annotations

from typing import Tuple

from src.trading_config import ORDER_TYPE, PAPER_CONSERVATIVE_FILLS


def fill_prices(
    last_price_cents: int,
    yes_bid: int = 0,
    yes_ask: int = 0,
    order_type: str = "",
    is_paper: bool = True,
) -> Tuple[int, int]:
    """Return (yes_fill_cents, no_fill_cents) — what each side actually costs.

    Taker crosses the spread: YES pays the ask, NO pays `100 - bid`, because
    buying NO is selling YES into the bid.

    Maker posts inside the spread and only earns that price if the order fills;
    paper trading assumes it does not and prices at the touch instead. That
    assumption is deliberately pessimistic and stays until the shadow-mode
    capture and frequency floors say otherwise.
    """
    order_type = order_type or ORDER_TYPE

    if yes_bid > 0 and yes_ask > 0:
        if order_type == "maker" and not (is_paper and PAPER_CONSERVATIVE_FILLS):
            # Post one cent inside the spread, never through the far side.
            return min(yes_bid + 1, yes_ask), min(100 - yes_ask + 1, 100 - yes_bid)
        return yes_ask, 100 - yes_bid

    # No book. The last trade is the only price we have, and it is the same on
    # both sides by construction — which is precisely why it must not be
    # preferred over a real quote.
    return last_price_cents, 100 - last_price_cents
