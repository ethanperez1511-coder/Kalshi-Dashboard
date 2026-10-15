"""The research harness must not see outcomes settled after a forecast was issued.

A lead-3 forecast for day T is issued on T-3. The outcomes for T-2 and T-1 do
not exist yet. Here they are planted as +30F errors: any candidate that
moves off a zero bias has looked ahead. (The repo's own score_rolling fits on
target < T, which would include them; harmless over 90 days, but a 5-day
recency window would be built mostly out of the future.)
"""
from __future__ import annotations

import datetime as dt

import pytest

# Research-only dependencies, not part of the trading install.
pytest.importorskip("numpy")
pytest.importorskip("scipy")

from research.weather_assessment.harness import Pair, information_set, predict_all  # noqa: E402


def _cell(lead=3, days=120):
    end = dt.date(2026, 6, 30)
    pairs = []
    for k in range(days, -1, -1):
        target = end - dt.timedelta(days=k)
        obs = 70.0 + (1 if k % 2 else -1) * 0.5           # small honest errors
        if k in (1, 2):                                   # settle AFTER the last pair's issue
            obs = 100.0
        pairs.append(Pair("KTST", lead, target, 70.0, 70.0, 2.0, obs))
    return pairs


def test_the_information_set_stops_the_day_before_issue():
    pairs = _cell()
    last = len(pairs) - 1
    hist = information_set(pairs, last)
    issued = pairs[last].issued
    assert max(h.target for h in hist) == issued - dt.timedelta(days=1)
    assert all(h.obs < 100 for h in hist)


def test_no_candidate_moves_on_outcomes_from_the_future():
    pairs = _cell()
    for i in range(len(pairs)):
        hist = information_set(pairs, i)
        if len(hist) >= 2:
            dists = predict_all(pairs[i], hist)
    for name, d in dists.items():
        assert abs(d.mu - 70.0) < 0.3, (name, d.mu)   # a 2-day leak moves the 90d mean 0.67F


def test_the_market_blend_weight_is_the_exact_trailing_optimum():
    """Closed form against a brute-force grid, on synthetic events."""
    import random

    from research.weather_assessment.harness import market_blend

    rng = random.Random(1)
    start = dt.date(2026, 1, 1)
    events = []
    for k in range(120):
        day = start + dt.timedelta(days=k)
        for _ in range(10):
            truth = rng.random()
            o = int(rng.random() < truth)
            mkt = min(max(truth + rng.gauss(0, 0.1), 0), 1)
            mdl = min(max(truth + rng.gauss(0, 0.2), 0), 1)
            events.append((day, "KTST", "T", o, mkt, 0.02, {"M": mdl}))
    out = market_blend(events, ["M"])["M"]
    o, m, k, b, w, day = out["rows"][-1]
    hist = [e for e in events if day - dt.timedelta(days=91) <= e[0] <= day - dt.timedelta(days=2)]
    grid = min((i / 1000 for i in range(1001)),
               key=lambda g: sum((g * e[6]["M"] + (1 - g) * e[4] - e[3]) ** 2 for e in hist))
    assert abs(w - grid) < 0.002
    assert all(r[5] - dt.timedelta(days=2) >= min(e[0] for e in events) for r in out["rows"])
