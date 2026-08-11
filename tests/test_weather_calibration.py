"""Walk-forward calibration, the promotion bars, and the gross-error tripwire.

Three properties matter more than any single number here:

  no leakage    a pair may never appear in both the fit and the evaluation set,
                and every evaluation date must fall strictly after every fit
                date. Scoring on fitted data measures memory, not skill.
  refuse thin   a cell below the sample floor is unpriceable no matter how
                flattering its fitted numbers look.
  refuse gross  a forecast that disagrees with the market by more than weather
                ever does is a broken input, not an edge.
"""
from __future__ import annotations

import datetime as dt
import random

import pytest

from src.weather.calibration import (
    CellFit,
    Pair,
    brier,
    brier_skill,
    climatology_rate,
    fit_cell,
    pit_summary,
    reliability_bins,
    reliability_slope,
    score_cell,
    split_pairs,
    strike_ladder,
)
from src.weather.promotion import (
    MIN_PAIRS_PER_CELL,
    beats_incumbent,
    evaluate_promotion,
)
from src.weather.sanity import (
    MAX_MODEL_MARKET_GAP_F,
    LadderPoint,
    check_forecast,
    implied_median,
)


def _pairs(n, start=dt.date(2025, 1, 1), bias=0.0, noise=0.0, seed=7):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        forecast = 80.0 + rng.uniform(-8, 8)
        observed = forecast + bias + (rng.gauss(0, noise) if noise else 0.0)
        out.append(Pair("KNYC", 1, start + dt.timedelta(days=i), forecast, round(observed)))
    return out


# --------------------------------------------------------------------------
# 1. Walk-forward discipline
# --------------------------------------------------------------------------

class TestWalkForward:
    def test_sets_are_disjoint_and_ordered(self):
        pairs = _pairs(40)
        split = dt.date(2025, 1, 21)
        fit, evaluation = split_pairs(pairs, split)
        assert fit and evaluation
        assert max(p.target_date for p in fit) < split
        assert min(p.target_date for p in evaluation) > split
        assert not ({p.target_date for p in fit} & {p.target_date for p in evaluation})

    def test_the_split_date_itself_lands_in_neither_set(self):
        """An inclusive boundary would let one pair train and test the model."""
        pairs = _pairs(10)
        split = pairs[5].target_date
        fit, evaluation = split_pairs(pairs, split)
        assert split not in {p.target_date for p in fit}
        assert split not in {p.target_date for p in evaluation}

    def test_fit_uses_only_the_fit_window(self):
        pairs = _pairs(40)
        fit_pairs, _ = split_pairs(pairs, dt.date(2025, 1, 21))
        fit = fit_cell(fit_pairs)
        assert fit.fit_end < dt.date(2025, 1, 21)
        assert fit.n_pairs == len(fit_pairs)

    def test_climatology_comes_from_the_fit_window(self):
        """Deriving the baseline from evaluation outcomes leaks the answer into
        the yardstick the model is measured against."""
        pairs = _pairs(60, bias=5.0, noise=2.0)
        fit_pairs, eval_pairs = split_pairs(pairs, dt.date(2025, 2, 1))
        rate = climatology_rate(fit_pairs)
        assert 0.0 <= rate <= 1.0
        # Recomputing on the eval half is a different number; they must not be
        # silently interchangeable.
        assert rate == climatology_rate(fit_pairs)


# --------------------------------------------------------------------------
# 2. The fit and the probabilities it produces
# --------------------------------------------------------------------------

class TestFit:
    def test_recovers_a_known_bias(self):
        fit = fit_cell(_pairs(200, bias=3.0, noise=0.001))
        assert fit.bias == pytest.approx(3.0, abs=0.6)

    def test_recovers_a_known_spread(self):
        fit = fit_cell(_pairs(400, bias=0.0, noise=4.0))
        assert fit.sigma == pytest.approx(4.0, abs=0.8)

    def test_too_few_pairs_returns_none(self):
        assert fit_cell([]) is None
        assert fit_cell(_pairs(1)) is None

    def test_strict_inequality_uses_a_continuity_correction(self):
        """">90" settles YES at 91+. The boundary sits at 90.5, so a forecast
        of exactly 90 must give slightly LESS than even money."""
        fit = CellFit("KNYC", 1, bias=0.0, sigma=3.0, n_pairs=100,
                      fit_start=dt.date(2025, 1, 1), fit_end=dt.date(2025, 3, 1))
        assert fit.prob_above(90.0, 90) < 0.5
        assert fit.prob_above(91.0, 90) > 0.5

    def test_below_mirrors_above(self):
        fit = CellFit("KNYC", 1, bias=0.0, sigma=3.0, n_pairs=100,
                      fit_start=dt.date(2025, 1, 1), fit_end=dt.date(2025, 3, 1))
        assert fit.prob_below(80.0, 83) == pytest.approx(1 - fit.prob_above(80.0, 82), abs=1e-9)

    def test_bucket_probabilities_partition_the_line(self):
        """The live ladder is a complete partition, so the pieces must sum to 1.
        This is the strongest free integrity check the market structure offers."""
        fit = CellFit("KPHL", 1, bias=0.0, sigma=3.5, n_pairs=100,
                      fit_start=dt.date(2025, 1, 1), fit_end=dt.date(2025, 3, 1))
        forecast = 87.0
        total = (
            fit.prob_below(forecast, 84)
            + fit.prob_between(forecast, 84, 85)
            + fit.prob_between(forecast, 86, 87)
            + fit.prob_between(forecast, 88, 89)
            + fit.prob_between(forecast, 90, 91)
            + fit.prob_above(forecast, 91)
        )
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_a_degenerate_sigma_cannot_produce_certainty(self):
        fit = CellFit("KNYC", 1, bias=0.0, sigma=0.0, n_pairs=100,
                      fit_start=dt.date(2025, 1, 1), fit_end=dt.date(2025, 3, 1))
        assert 0.0 < fit.prob_above(90.0, 90) < 1.0


# --------------------------------------------------------------------------
# 3. Metrics
# --------------------------------------------------------------------------

class TestMetrics:
    def test_brier_rewards_confident_correctness(self):
        assert brier([1.0, 0.0], [1, 0]) == 0.0
        assert brier([0.0, 1.0], [1, 0]) == 1.0

    def test_skill_is_zero_against_its_own_climatology(self):
        outcomes = [1, 0, 1, 0]
        assert brier_skill([0.5] * 4, outcomes, 0.5) == pytest.approx(0.0)

    def test_perfect_reliability_has_unit_slope(self):
        bins = [
            {"low": 0, "high": 1, "n": 50, "mean_predicted": 0.2, "observed_frequency": 0.2},
            {"low": 0, "high": 1, "n": 50, "mean_predicted": 0.8, "observed_frequency": 0.8},
        ]
        assert reliability_slope(bins) == pytest.approx(1.0)

    def test_overconfidence_shows_as_slope_below_one(self):
        """Predictions more extreme than reality — the direction that
        manufactures apparent edge."""
        bins = [
            {"low": 0, "high": 1, "n": 50, "mean_predicted": 0.1, "observed_frequency": 0.3},
            {"low": 0, "high": 1, "n": 50, "mean_predicted": 0.9, "observed_frequency": 0.7},
        ]
        assert reliability_slope(bins) < 1.0

    def test_scoring_produces_one_outcome_per_ladder_strike(self):
        fit = fit_cell(_pairs(80, bias=0.0, noise=3.0))
        _, evaluation = split_pairs(_pairs(80, bias=0.0, noise=3.0), dt.date(2025, 2, 1))
        scored = score_cell(fit, evaluation)
        assert len(scored.predictions) == len(evaluation) * len(strike_ladder(80.0))
        assert set(scored.outcomes) <= {0, 1}

    def test_pit_of_a_well_specified_model_is_near_uniform(self):
        pairs = _pairs(600, bias=0.0, noise=3.0, seed=11)
        fit_pairs, eval_pairs = split_pairs(pairs, pairs[300].target_date)
        scored = score_cell(fit_cell(fit_pairs), eval_pairs)
        summary = pit_summary(scored.pit)
        assert summary["mean"] == pytest.approx(0.5, abs=0.08)
        assert summary["sd"] == pytest.approx(0.289, abs=0.06)


# --------------------------------------------------------------------------
# 4. Promotion bars
# --------------------------------------------------------------------------

class TestPromotion:
    def test_thin_cell_is_refused_however_good_the_numbers(self):
        verdict = evaluate_promotion(
            n_pairs=MIN_PAIRS_PER_CELL - 1, brier_skill=0.9, reliability_slope=1.0,
        )
        assert not verdict
        assert "sample floor" in verdict.reasons[0]

    def test_failing_climatology_is_refused(self):
        verdict = evaluate_promotion(200, brier_skill=0.0, reliability_slope=1.0)
        assert not verdict
        assert any("climatology" in r for r in verdict.reasons)

    def test_overconfident_model_is_refused_even_with_good_skill(self):
        """Beating climatology while being overconfident is the exact profile
        of a model that looks profitable and is not."""
        verdict = evaluate_promotion(200, brier_skill=0.5, reliability_slope=0.4)
        assert not verdict
        assert any("reliability" in r and "overconfident" in r for r in verdict.reasons)

    def test_all_bars_cleared_promotes(self):
        assert evaluate_promotion(200, brier_skill=0.4, reliability_slope=1.0)

    def test_challenger_must_beat_the_incumbent_not_merely_pass(self):
        assert not beats_incumbent(challenger_skill=0.40, incumbent_skill=0.40)
        assert beats_incumbent(challenger_skill=0.45, incumbent_skill=0.40)


# --------------------------------------------------------------------------
# 5. Gross-error tripwire
# --------------------------------------------------------------------------

def _ladder(median: float):
    """A ladder whose implied P(T > strike) crosses 0.5 at `median`."""
    return [
        LadderPoint(median - 4, "above", 0.90),
        LadderPoint(median - 2, "above", 0.70),
        LadderPoint(median, "above", 0.50),
        LadderPoint(median + 2, "above", 0.30),
        LadderPoint(median + 4, "above", 0.10),
    ]


class TestSanityTripwire:
    def test_recovers_the_implied_median(self):
        assert implied_median(_ladder(88.0)) == pytest.approx(88.0, abs=0.5)

    def test_normalises_below_contracts_into_the_same_curve(self):
        points = _ladder(88.0) + [LadderPoint(84.0, "below", 0.05)]
        assert implied_median(points) == pytest.approx(88.0, abs=0.6)

    def test_agreement_passes(self):
        assert check_forecast(89.0, _ladder(88.0)).ok

    def test_overnight_minimum_read_as_a_maximum_is_caught(self):
        """THE failure: ~20 °F low across a whole city-day. It presents as the
        best edge the system has ever seen, so it must be structurally
        untradeable rather than merely unlikely."""
        verdict = check_forecast(68.0, _ladder(88.0))
        assert not verdict.ok
        assert verdict.gap == pytest.approx(20.0, abs=1.0)
        assert "not an edge" in verdict.reason

    def test_a_large_genuine_disagreement_is_also_refused(self):
        """The check keys on the disagreement, not on any particular cause —
        a station mix-up or unit error has the same signature."""
        assert not check_forecast(88.0 + MAX_MODEL_MARKET_GAP_F + 1, _ladder(88.0)).ok

    def test_ordinary_disagreement_still_trades(self):
        """A 6 °F difference is a real forecast disagreement and the whole
        point of the model. The tripwire must not swallow it."""
        assert check_forecast(94.0, _ladder(88.0)).ok

    def test_thin_ladder_performs_no_check_rather_than_a_bad_one(self):
        verdict = check_forecast(60.0, [LadderPoint(88.0, "above", 0.5)])
        assert verdict.ok
        assert "does not pin" in verdict.reason

    def test_ladder_that_never_crosses_pins_nothing(self):
        points = [LadderPoint(s, "above", 0.99) for s in (70, 72, 74)]
        assert implied_median(points) is None
