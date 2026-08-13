from __future__ import annotations

import math
from dataclasses import dataclass

from src.ev.fills import fill_prices
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
    # The prices the numbers above were computed against. Stored on the trade
    # so an autopsy is a lookup rather than a reconstruction.
    yes_fill_cents: int = 0
    no_fill_cents: int = 0

    @property
    def best_fill_cents(self) -> int:
        """Price of the recommended side, in that side's own terms."""
        return self.yes_fill_cents if self.recommended_side == "yes" else self.no_fill_cents

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
    is_paper: bool = True,
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

    # One shared source of fill prices, so the price that justifies a trade and
    # the price the trade costs cannot drift apart. They did, by one cent, and
    # that cent was the whole margin on trade 1/50.
    yes_fill, no_fill = fill_prices(price_cents, yes_bid, yes_ask, ot, is_paper)

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

    # No side: buy NO at price_no. Pay price_no, receive 1 if the market
    # resolves NO. So the win is (1 - price_no) with probability (1 - p) and
    # the loss is price_no with probability p.
    #
    # This read `(1-p) * price_no - p * (1-price_no)` — the win and loss
    # amounts swapped, which reduces to `price_no - p` and is not an expected
    # value at all. It reported +0.50 on a trade whose true EV is -0.10, and
    # the error grew with how expensive NO was, so it manufactured enormous
    # fake EV on exactly the cheap-YES longshot fades this system trades and
    # dragged `recommended_side` to NO along with it. Verified against a
    # 400k-trial simulation; the corrected form matches to three decimals and
    # collapses to `(1-p) - price_no`, the same identity the YES side has.
    raw_ev_no = (1.0 - p) * (1.0 - price_no) - p * price_no
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
        yes_fill_cents=yes_fill,
        no_fill_cents=no_fill,
    )
