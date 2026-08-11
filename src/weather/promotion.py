"""Every bar a weather predictor must clear before it may price real money.

All promotion bars live here, together, so that "is this model allowed to
trade?" is one auditable place rather than a judgement spread across modules.
Promotion is a measurement, never an opinion.

Bars, in the order they apply:

  SAMPLE FLOOR   a (station, lead) cell with too few pairs is unpriceable no
                 matter how good its fitted numbers look.
  CLIMATOLOGY    the model must beat "just use the base rate" on HELD-OUT
                 pairs. A model that cannot is not adding information.
  RELIABILITY    stated probabilities must match observed frequencies. A model
                 can beat climatology while being systematically overconfident,
                 and overconfidence is exactly what reads as tradeable edge.
  CHALLENGER     any replacement predictor must beat the incumbent's Brier
                 skill on the same held-out window (see CHALLENGERS below).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

# --- Sample floor ---------------------------------------------------------
# Two parameters are fitted per cell (bias and sd). The standard error of an sd
# estimate is sd/sqrt(2(n-1)), so n=60 puts it near 9% relative — tight enough
# that a mis-fitted sd cannot masquerade as edge — and 60 daily pairs is about
# two months, enough that a single unusual week cannot dominate a cell.
# Below this the cell is unpriceable regardless of what the fit returns.
MIN_PAIRS_PER_CELL = 60

# --- Climatology bar ------------------------------------------------------
# Brier skill vs climatology on held-out pairs. Zero means "no better than the
# base rate"; require a real margin rather than a coin-flip's worth.
MIN_BRIER_SKILL = 0.05

# --- Reliability bar ------------------------------------------------------
# Observed frequency regressed on predicted probability. 1.0 is perfect; below
# 1 means overconfident (predictions too extreme for the outcomes), which is
# the dangerous direction because it manufactures apparent edge.
MIN_RELIABILITY_SLOPE = 0.80
MAX_RELIABILITY_SLOPE = 1.25

# --- Challenger bar -------------------------------------------------------
# A replacement predictor must BEAT the incumbent on the same held-out window,
# not merely pass the absolute bars. Registered here so every future candidate
# faces one rule:
#   NWS gridpoint  — pending enough self-archived pairs (migration path off the
#                    third-party MOS archive)
#   GEFS ensemble  — pending the dynamical.org probe in tasks/todo.md
CHALLENGER_MIN_SKILL_IMPROVEMENT = 0.01


@dataclass
class PromotionVerdict:
    promoted: bool
    reasons: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.promoted


def evaluate_promotion(
    n_pairs: int,
    brier_skill: float,
    reliability_slope: float,
) -> PromotionVerdict:
    """Decide whether a fitted cell may price above the price-derived tier.

    Every input must come from HELD-OUT pairs. Scoring a model on the data it
    was fitted to measures memory, not skill, and would promote a model that
    has learned nothing.
    """
    reasons: List[str] = []

    if n_pairs < MIN_PAIRS_PER_CELL:
        reasons.append(
            f"sample floor: {n_pairs} held-out pairs < {MIN_PAIRS_PER_CELL}"
        )
    if brier_skill < MIN_BRIER_SKILL:
        reasons.append(
            f"climatology bar: Brier skill {brier_skill:.3f} < {MIN_BRIER_SKILL}"
        )
    if not (MIN_RELIABILITY_SLOPE <= reliability_slope <= MAX_RELIABILITY_SLOPE):
        direction = "overconfident" if reliability_slope < MIN_RELIABILITY_SLOPE else "underconfident"
        reasons.append(
            f"reliability bar: slope {reliability_slope:.3f} outside "
            f"[{MIN_RELIABILITY_SLOPE}, {MAX_RELIABILITY_SLOPE}] ({direction})"
        )

    return PromotionVerdict(promoted=not reasons, reasons=reasons)


def beats_incumbent(challenger_skill: float, incumbent_skill: float) -> bool:
    """Is a challenger predictor good enough to replace the incumbent?"""
    return (challenger_skill - incumbent_skill) >= CHALLENGER_MIN_SKILL_IMPROVEMENT
