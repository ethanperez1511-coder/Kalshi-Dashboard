"""A structural tripwire against forecasts that are wrong by a whole regime.

MOS reports daily maxima and overnight minima in the same `n_x` field,
distinguished only by valid time. Reading a 12Z row as a daily max puts the
forecast roughly 20 °F under truth — and that does not look like a bug
downstream. It looks like every contract in the city is mispriced in the same
direction, which is to say it looks like the best edge the system has ever
found, and the sizing engine would act on it.

Parsing this correctly is necessary but not sufficient: the same signature —
model and market disagreeing by more than weather ever does — would also be
produced by a unit error, a station mix-up, a stale forecast, or a future
refactor of the parser. So the check lives at the model layer and keys on the
disagreement itself rather than on any particular cause.

The market is the reference because these ladders are liquid and the whole
ladder cannot be wrong at once: a contract at 90° and one at 84° must price
consistently with a single underlying temperature, so the ladder pins the
market's implied median tightly.

A station-day that trips this is UNPRICEABLE and reported. It is never
downweighted and never traded on a wider spread — the entire failure mode is
that the disagreement is enormous, so accommodating a large disagreement would
defeat the purpose.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Fitted forecast error at leads 1-3 has sd of roughly 3-4 °F, so a genuine
# disagreement of 12 °F is already a ~3-sigma event. The failure mode this
# guards against produces ~20 °F. The gap between those two numbers is the
# working room, and it is deliberately wide: this is a tripwire for gross
# error, not a tuning knob for marginal trades.
MAX_MODEL_MARKET_GAP_F = 12.0

# Below this the ladder is too thin to pin an implied median, so there is
# nothing to check against and the day is left alone.
MIN_LADDER_POINTS = 3


@dataclass(frozen=True)
class LadderPoint:
    """One contract on a city-day ladder: strike, direction, market probability."""
    strike: float
    direction: str   # "above" | "below"
    market_prob: float

    def prob_above(self) -> float:
        """Market P(T > strike), normalised out of the contract's direction."""
        return self.market_prob if self.direction == "above" else 1.0 - self.market_prob


@dataclass(frozen=True)
class SanityVerdict:
    ok: bool
    implied_median: Optional[float] = None
    predicted: Optional[float] = None
    gap: Optional[float] = None
    reason: str = ""


def implied_median(points: Sequence[LadderPoint]) -> Optional[float]:
    """Temperature at which the market's implied P(T > x) crosses 0.5.

    Linear interpolation between the two strikes that straddle the crossing.
    Returns None when the ladder never crosses — an all-in-the-tails ladder
    pins nothing, and extrapolating a median from it would invent the very
    reference this check depends on.
    """
    usable = sorted(
        [p for p in points if 0.0 <= p.market_prob <= 1.0],
        key=lambda p: p.strike,
    )
    if len(usable) < MIN_LADDER_POINTS:
        return None

    curve: List[Tuple[float, float]] = [(p.strike, p.prob_above()) for p in usable]
    for (x0, p0), (x1, p1) in zip(curve, curve[1:]):
        if (p0 - 0.5) * (p1 - 0.5) <= 0 and p0 != p1:
            return x0 + (p0 - 0.5) * (x1 - x0) / (p0 - p1)
    return None


def check_forecast(
    predicted_max: float, points: Sequence[LadderPoint],
) -> SanityVerdict:
    """Is our predicted max consistent with what the market is pricing?"""
    median = implied_median(points)
    if median is None:
        return SanityVerdict(
            ok=True, predicted=predicted_max,
            reason="ladder does not pin an implied median; no check performed",
        )

    gap = abs(predicted_max - median)
    if gap > MAX_MODEL_MARKET_GAP_F:
        direction = "below" if predicted_max < median else "above"
        return SanityVerdict(
            ok=False, implied_median=median, predicted=predicted_max, gap=gap,
            reason=(
                f"predicted max {predicted_max:.1f}°F is {gap:.1f}°F {direction} "
                f"the market-implied median {median:.1f}°F (limit "
                f"{MAX_MODEL_MARKET_GAP_F}°F). A disagreement this large is a "
                f"broken input — overnight minimum read as a maximum, wrong "
                f"station, wrong units, or a stale run — not an edge."
            ),
        )
    return SanityVerdict(
        ok=True, implied_median=median, predicted=predicted_max, gap=gap,
        reason="consistent with market-implied median",
    )
