from __future__ import annotations

import math
from dataclasses import dataclass

from src.trading_config import ORDER_TYPE


def kalshi_taker_fee(price_cents: int) -> float:
    """Kalshi taker fee per contract in dollars.

    Formula: ceil_to_cent(0.07 * price * (1 - price))
    where price is in [0, 1].  Returns dollars (e.g. 0.02).
    Maker fee is 0 on standard markets.
    """
    price = price_cents / 100.0
    raw = 0.07 * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def fee_per_contract(price_cents: int, order_type: str = "") -> float:
    """Return fee per contract in dollars based on order type."""
    ot = order_type or ORDER_TYPE
    if ot == "maker":
        return 0.0
    return kalshi_taker_fee(price_cents)


@dataclass
class EVResult:
    """Expected-value calculation result for both Yes and No sides of a market."""

    p_model: float
    implied_prob: float
    edge: float          # Yes-side edge: p_model - price
    no_edge: float       # No-side edge: (1 - p_model) - (1 - price)
    raw_ev: float        # Raw EV for Yes side
    net_ev: float        # Net EV for Yes side after fees
    no_ev: float         # Net EV for No side after fees
    recommended_side: str  # "yes" or "no"
    fee_rate: float

    @property
    def best_edge(self) -> float:
        """Return the edge for the recommended side."""
        return self.edge if self.recommended_side == "yes" else self.no_edge

    @property
    def best_ev(self) -> float:
        """Return the net EV for the recommended side."""
        return self.net_ev if self.recommended_side == "yes" else self.no_ev


def calculate_ev(
    p_model: float,
    price_cents: int,
    fee_rate: float = -1.0,
    order_type: str = "",
    yes_bid: int = 0,
    yes_ask: int = 0,
) -> EVResult:
    """Calculate expected value for both Yes and No sides of a Kalshi market.

    Parameters
    ----------
    p_model:
        Model probability that the market resolves Yes (in [0, 1]).
    price_cents:
        Current Yes price in cents (integer in [0, 100]).
    fee_rate:
        Deprecated. If >= 0 it overrides the real fee formula (for backwards compat).
        Default -1 means use Kalshi's real formula.
    order_type:
        "maker" or "taker". Defaults to trading_config.ORDER_TYPE.
    yes_bid, yes_ask:
        Bid and ask in cents. When provided and order_type is "taker",
        the fill prices account for crossing the spread.

    Returns
    -------
    EVResult with fully populated Yes/No analysis and recommended side.
    """
    ot = order_type or ORDER_TYPE

    # Determine fill prices: taker crosses spread, maker posts limit
    if yes_bid > 0 and yes_ask > 0 and ot == "taker":
        yes_fill = yes_ask   # taker buys at ask
        no_fill = 100 - yes_bid   # taker buys No at 100 - bid
    else:
        yes_fill = price_cents
        no_fill = 100 - price_cents

    price_yes = yes_fill / 100.0
    price_no = no_fill / 100.0
    p = p_model

    # Fee calculation
    if fee_rate >= 0:
        # Legacy flat fee path
        fee_yes = fee_rate
        fee_no = fee_rate
    else:
        fee_yes = fee_per_contract(yes_fill, ot)
        fee_no = fee_per_contract(no_fill, ot)

    # Yes side: buy YES at price_yes
    raw_ev_yes = p * (1.0 - price_yes) - (1.0 - p) * price_yes
    net_ev_yes = raw_ev_yes - fee_yes
    edge_yes = p - price_yes

    # No side: buy NO at price_no
    raw_ev_no = (1.0 - p) * price_no - p * (1.0 - price_no)
    net_ev_no = raw_ev_no - fee_no
    edge_no = (1.0 - p) - price_no

    recommended_side = "yes" if net_ev_yes >= net_ev_no else "no"

    # Use the mid price for implied_prob (informational)
    implied_prob = price_cents / 100.0

    return EVResult(
        p_model=p_model,
        implied_prob=implied_prob,
        edge=edge_yes,
        no_edge=edge_no,
        raw_ev=raw_ev_yes,
        net_ev=net_ev_yes,
        no_ev=net_ev_no,
        recommended_side=recommended_side,
        fee_rate=fee_yes,
    )
