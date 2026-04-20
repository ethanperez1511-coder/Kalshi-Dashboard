from __future__ import annotations

from dataclasses import dataclass


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
    fee_rate: float = 0.01,
) -> EVResult:
    """Calculate expected value for both Yes and No sides of a Kalshi market.

    Parameters
    ----------
    p_model:
        Model probability that the market resolves Yes (in [0, 1]).
    price_cents:
        Current Yes price in cents (integer in [0, 100]).
    fee_rate:
        Flat fee applied to both sides' net EV (default 1 cent per dollar).

    Returns
    -------
    EVResult with fully populated Yes/No analysis and recommended side.
    """
    price = price_cents / 100.0
    p = p_model

    # Yes side
    raw_ev_yes = p * (1.0 - price) - (1.0 - p) * price
    net_ev_yes = raw_ev_yes - fee_rate
    edge_yes = p - price

    # No side: buying No at (1 - price)
    raw_ev_no = (1.0 - p) * price - p * (1.0 - price)
    net_ev_no = raw_ev_no - fee_rate
    edge_no = (1.0 - p) - (1.0 - price)

    recommended_side = "yes" if net_ev_yes >= net_ev_no else "no"

    return EVResult(
        p_model=p_model,
        implied_prob=price,
        edge=edge_yes,
        no_edge=edge_no,
        raw_ev=raw_ev_yes,
        net_ev=net_ev_yes,
        no_ev=net_ev_no,
        recommended_side=recommended_side,
        fee_rate=fee_rate,
    )
