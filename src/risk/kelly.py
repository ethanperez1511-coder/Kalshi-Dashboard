from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass
class KellyResult:
    full_kelly: float           # Full Kelly fraction of bankroll
    fractional_kelly: float     # Adjusted Kelly (e.g., quarter Kelly)
    recommended_dollars: float  # Dollar amount to risk
    recommended_quantity: int   # Number of contracts (rounded down)
    edge: float
    odds: float


def kelly_size(
    p_model: float,
    price_cents: int,
    bankroll: float,
    kelly_fraction: float = 0.25,
) -> KellyResult:
    """Calculate position size using fractional Kelly criterion.

    Kelly formula: f* = (bp - q) / b
    where b = odds (payout ratio), p = win prob, q = 1-p
    """
    price = price_cents / 100.0
    edge = p_model - price

    if edge <= 0:
        return KellyResult(
            full_kelly=0, fractional_kelly=0,
            recommended_dollars=0, recommended_quantity=0,
            edge=edge, odds=0,
        )

    # For binary contracts: pay `price`, win `1-price` if correct
    # odds = (1 - price) / price = net payout per dollar risked
    b = (1 - price) / price
    p = p_model
    q = 1 - p

    # Kelly: f* = (bp - q) / b
    full_kelly_frac = (b * p - q) / b
    full_kelly_frac = max(0, full_kelly_frac)

    fractional = full_kelly_frac * kelly_fraction
    dollars = bankroll * fractional

    # Convert to contract quantity: each contract costs `price` dollars
    contract_cost = price
    quantity = math.floor(dollars / contract_cost) if contract_cost > 0 else 0

    return KellyResult(
        full_kelly=full_kelly_frac,
        fractional_kelly=fractional,
        recommended_dollars=round(dollars, 2),
        recommended_quantity=quantity,
        edge=edge,
        odds=b,
    )
