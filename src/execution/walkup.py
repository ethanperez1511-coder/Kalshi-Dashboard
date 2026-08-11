"""Walk-up schedule for a resting maker order, and the cap that bounds it.

The cap is the load-bearing part. Walking the price up improves the chance of
filling and spends the edge that justified the trade; past a certain price the
trade would no longer have passed the EV filter at all. Stepping beyond that
would mean execution deciding WHETHER we trade, which it may never do.

So the ceiling is `p_model` minus the edge the filter required, expressed in
the order's own side-cost cents. An order that would have to cross it is
cancelled with its remainder unfilled — never filled at the cap, never nudged
one more cent "since we are nearly there".

Every parameter is config with the current value as default: these are exactly
the knobs the Phase 4 experiment framework should sweep.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from src.trading_config import (
    MAKER_MAX_STEPS,
    MAKER_REST_SECONDS,
    MAKER_STEP_CENTS,
    MAKER_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalkStep:
    index: int
    price_cents: int
    rest_until_seconds: float


@dataclass(frozen=True)
class WalkPlan:
    steps: List[WalkStep]
    cap_cents: int
    capped_early: bool          # the cap, not MAX_STEPS, ended the walk
    reason: str

    @property
    def final_price(self) -> Optional[int]:
        return self.steps[-1].price_cents if self.steps else None


def max_price_cents(
    p_model: float, required_edge: float, side: str,
) -> int:
    """Highest price this order may pay and still be the trade that was approved.

    Filter logic works in YES-probability terms, so a NO order's ceiling is the
    complement: paying more than `1 - (p_model + required_edge)` for NO is the
    same overpayment as paying more than `p_model - required_edge` for YES.

    Floored, never rounded: rounding up would hand back a fraction of a cent of
    the very edge this exists to protect.
    """
    if side == "yes":
        limit = Decimal(str(p_model)) - Decimal(str(required_edge))
    else:
        limit = Decimal("1") - (Decimal(str(p_model)) + Decimal(str(required_edge)))
    cents = int((limit * 100).to_integral_value(rounding="ROUND_FLOOR"))
    return max(0, min(100, cents))


def build_plan(
    start_price_cents: int,
    p_model: float,
    required_edge: float,
    side: str,
    step_cents: int = MAKER_STEP_CENTS,
    max_steps: int = MAKER_MAX_STEPS,
    rest_seconds: float = MAKER_REST_SECONDS,
    timeout_seconds: float = MAKER_TIMEOUT_SECONDS,
) -> WalkPlan:
    """Prices this order may rest at, in order, stopping at the cap.

    The first step is the starting price itself. A plan whose very first price
    already exceeds the cap has NO steps — the order is never placed, because
    there is no price at which it is still the approved trade.
    """
    cap = max_price_cents(p_model, required_edge, side)

    steps: List[WalkStep] = []
    elapsed = 0.0
    capped_early = False

    for index in range(max_steps + 1):
        price = start_price_cents + index * step_cents
        if price > cap:
            capped_early = True
            break
        if elapsed > timeout_seconds:
            break
        elapsed += rest_seconds
        steps.append(WalkStep(index=index, price_cents=price, rest_until_seconds=elapsed))

    if not steps:
        reason = (
            f"not placed: starting price {start_price_cents}c already exceeds the "
            f"cap {cap}c (p_model {p_model:.3f} minus required edge "
            f"{required_edge:.3f}) — there is no price at which this is still "
            f"the approved trade"
        )
    elif capped_early:
        reason = (
            f"walk stops at {steps[-1].price_cents}c: the next step would cross "
            f"the cap {cap}c and spend the edge that justified the trade"
        )
    else:
        reason = f"walk exhausts {len(steps)} steps below the cap {cap}c"

    return WalkPlan(steps=steps, cap_cents=cap, capped_early=capped_early, reason=reason)
