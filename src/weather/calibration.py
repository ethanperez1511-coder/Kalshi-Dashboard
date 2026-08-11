"""Fit forecast error, then score the result on data it has never seen.

The model is a normal distribution centred on the MOS forecast, corrected for
that cell's historical bias, with a width fitted from that cell's historical
error. Dispersion is therefore ESTIMATED FROM PAST ERROR, NOT OBSERVED — the
forecast carries no uncertainty of its own, so this cannot tell a confident day
from an uncertain one. That is the known limitation and the reason an ensemble
challenger is queued.

Walk-forward is enforced structurally, not by convention: `split_pairs` returns
disjoint fit and evaluation sets with every evaluation date strictly after
every fit date, and the scoring functions accept only the evaluation half.
Scoring a model on the pairs it was fitted to measures memory rather than
skill, and would promote a model that has learned nothing.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_NORMAL = NormalDist()
# Below this the fitted width is not credible — identical forecasts every day
# would imply a perfect model, which is never true of weather.
_MIN_SIGMA = 0.5


@dataclass(frozen=True)
class Pair:
    """One forecast and the outcome it was trying to predict."""
    station: str
    lead_days: int
    target_date: dt.date
    forecast_f: float
    observed_f: float

    @property
    def error(self) -> float:
        return self.observed_f - self.forecast_f


@dataclass(frozen=True)
class CellFit:
    """Fitted error model for one (station, lead) cell."""
    station: str
    lead_days: int
    bias: float        # mean(observed - forecast); added to the raw forecast
    sigma: float       # sd of that error
    n_pairs: int
    fit_start: dt.date
    fit_end: dt.date

    def predict_mean(self, forecast_f: float) -> float:
        return forecast_f + self.bias

    def prob_above(self, forecast_f: float, strike: float) -> float:
        """P(observed max > strike), for a contract that settles YES above it.

        Settlement is whole degrees and the inequality is strict, so ">90"
        means "91 or more". The continuity correction at 90.5 is what makes the
        integer boundary right; without it every at-the-money strike is
        mispriced by roughly half a degree of probability mass.
        """
        mu = self.predict_mean(forecast_f)
        z = (strike + 0.5 - mu) / max(self.sigma, _MIN_SIGMA)
        return 1.0 - _NORMAL.cdf(z)

    def prob_below(self, forecast_f: float, strike: float) -> float:
        """P(observed max < strike) — "<83" means "82 or less"."""
        mu = self.predict_mean(forecast_f)
        z = (strike - 0.5 - mu) / max(self.sigma, _MIN_SIGMA)
        return _NORMAL.cdf(z)

    def prob_between(self, forecast_f: float, low: float, high: float) -> float:
        """P(low <= observed <= high) for a bucket contract.

        Four of every six live contracts are these, and they are why the model
        must produce a distribution rather than a tail probability.
        """
        mu = self.predict_mean(forecast_f)
        sigma = max(self.sigma, _MIN_SIGMA)
        upper = _NORMAL.cdf((high + 0.5 - mu) / sigma)
        lower = _NORMAL.cdf((low - 0.5 - mu) / sigma)
        return max(0.0, upper - lower)


def fit_cell(pairs: Sequence[Pair]) -> Optional[CellFit]:
    """Bias and width for one cell, or None if there is nothing to fit.

    No floor is applied here — `promotion.MIN_PAIRS_PER_CELL` decides whether a
    fit may be USED. Keeping those separate means a thin cell still reports its
    numbers for inspection instead of vanishing.
    """
    if len(pairs) < 2:
        return None
    errors = [p.error for p in pairs]
    n = len(errors)
    bias = sum(errors) / n
    variance = sum((e - bias) ** 2 for e in errors) / (n - 1)
    dates = [p.target_date for p in pairs]
    return CellFit(
        station=pairs[0].station,
        lead_days=pairs[0].lead_days,
        bias=bias,
        sigma=math.sqrt(variance),
        n_pairs=n,
        fit_start=min(dates),
        fit_end=max(dates),
    )


def split_pairs(
    pairs: Sequence[Pair], split_date: dt.date,
) -> Tuple[List[Pair], List[Pair]]:
    """(fit, evaluation), disjoint, with every eval date strictly later.

    The boundary is exclusive on both sides on purpose: a pair dated exactly on
    the split would otherwise be usable as both training and test.
    """
    fit = [p for p in pairs if p.target_date < split_date]
    evaluation = [p for p in pairs if p.target_date > split_date]
    overlap = {p.target_date for p in fit} & {p.target_date for p in evaluation}
    assert not overlap, f"walk-forward violated: {sorted(overlap)[:3]} in both sets"
    return fit, evaluation


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

@dataclass
class Scored:
    """Predicted probabilities and their outcomes, on held-out pairs only."""
    predictions: List[float] = field(default_factory=list)
    outcomes: List[int] = field(default_factory=list)
    pit: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.predictions)


def strike_ladder(forecast_f: float, offsets: Iterable[int] = (-6, -4, -2, 0, 2, 4, 6)):
    """Strikes around the forecast, mimicking how Kalshi sets its ladders.

    Evaluating near the money is the honest test: strikes far into the tails
    are trivially predictable and would inflate every skill score.
    """
    base = round(forecast_f)
    return [base + o for o in offsets]


def score_cell(fit: CellFit, evaluation: Sequence[Pair]) -> Scored:
    """Score a fit on held-out pairs across a realistic strike ladder."""
    scored = Scored()
    sigma = max(fit.sigma, _MIN_SIGMA)
    for pair in evaluation:
        for strike in strike_ladder(pair.forecast_f):
            scored.predictions.append(fit.prob_above(pair.forecast_f, strike))
            scored.outcomes.append(1 if pair.observed_f > strike else 0)
        scored.pit.append(
            _NORMAL.cdf((pair.observed_f - fit.predict_mean(pair.forecast_f)) / sigma)
        )
    return scored


def brier(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    if not predictions:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def brier_skill(
    predictions: Sequence[float], outcomes: Sequence[int], climatology: float,
) -> float:
    """1 - BS/BS_climatology. Zero means no better than the base rate.

    `climatology` must come from the FIT window, never from the evaluation
    outcomes — deriving the baseline from the test set leaks the answer into
    the thing the model is being measured against.
    """
    if not predictions:
        return float("nan")
    reference = brier([climatology] * len(outcomes), outcomes)
    if reference <= 0:
        return float("nan")
    return 1.0 - brier(predictions, outcomes) / reference


def reliability_bins(
    predictions: Sequence[float], outcomes: Sequence[int], n_bins: int = 10,
) -> List[dict]:
    bins: List[dict] = []
    for i in range(n_bins):
        low, high = i / n_bins, (i + 1) / n_bins
        idx = [
            j for j, p in enumerate(predictions)
            if (low <= p < high) or (i == n_bins - 1 and p == 1.0)
        ]
        if not idx:
            continue
        bins.append({
            "low": low,
            "high": high,
            "n": len(idx),
            "mean_predicted": sum(predictions[j] for j in idx) / len(idx),
            "observed_frequency": sum(outcomes[j] for j in idx) / len(idx),
        })
    return bins


def reliability_slope(bins: Sequence[dict]) -> float:
    """Observed frequency regressed on predicted probability, weighted by bin n.

    1.0 is perfect. Below 1 is OVERCONFIDENT — predictions more extreme than
    reality — which is the direction that manufactures apparent edge, so it is
    the one the promotion bar is asymmetrically strict about.
    """
    usable = [b for b in bins if b["n"] > 0]
    if len(usable) < 2:
        return float("nan")
    total = sum(b["n"] for b in usable)
    mean_x = sum(b["mean_predicted"] * b["n"] for b in usable) / total
    mean_y = sum(b["observed_frequency"] * b["n"] for b in usable) / total
    numerator = sum(
        b["n"] * (b["mean_predicted"] - mean_x) * (b["observed_frequency"] - mean_y)
        for b in usable
    )
    denominator = sum(b["n"] * (b["mean_predicted"] - mean_x) ** 2 for b in usable)
    if denominator == 0:
        return float("nan")
    return numerator / denominator


def climatology_rate(fit_pairs: Sequence[Pair]) -> float:
    """Base rate of 'above the ladder strike', measured on the FIT window."""
    hits = total = 0
    for pair in fit_pairs:
        for strike in strike_ladder(pair.forecast_f):
            total += 1
            hits += 1 if pair.observed_f > strike else 0
    return (hits / total) if total else 0.5


def pit_summary(pit: Sequence[float]) -> Dict[str, float]:
    """A calibrated forecast gives uniform PIT: mean 0.5, sd ~0.289.

    A sd below that means the distribution is too wide; above it, too narrow.
    """
    if not pit:
        return {"mean": float("nan"), "sd": float("nan"), "n": 0}
    n = len(pit)
    mean = sum(pit) / n
    if n < 2:
        return {"mean": mean, "sd": float("nan"), "n": n}
    sd = math.sqrt(sum((x - mean) ** 2 for x in pit) / (n - 1))
    return {"mean": mean, "sd": sd, "n": n}
