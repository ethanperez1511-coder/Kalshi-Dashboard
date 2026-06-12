"""Kalshi trading-fee math.

Kalshi charges a per-fill trading fee of:

    fee = round_up_to_cent(rate * contracts * P * (1 - P))

where P is the fill price in dollars (0..1). The fee is largest near P=0.50
and vanishes near the extremes. Paper settlement subtracts this so paper PnL
reflects what live trading would actually net — never the rosier fee-free
number.
"""
from __future__ import annotations

import math

from src.trading_config import KALSHI_FEE_RATE


def kalshi_fee(quantity: int, price_cents: int, rate: float = KALSHI_FEE_RATE) -> float:
    """Trading fee in dollars for a fill of `quantity` at `price_cents`.

    price_cents is in the order's own side terms (0..100). Rounds up to the
    next whole cent, matching Kalshi's published formula.
    """
    if quantity <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0.0
    p = price_cents / 100.0
    raw = rate * quantity * p * (1.0 - p)
    return math.ceil(raw * 100.0) / 100.0
