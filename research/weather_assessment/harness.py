"""Walk-forward comparison of five weather-model candidates. Read-only.

    python -m research.weather_assessment.harness

Reads the cache written by fetch.py; writes results/ (CSV + markdown). Never
imports the trading path's state, never touches a database.

NO LOOKAHEAD, precisely. A forecast for target day T at lead L is issued on
I = T - L. Its information set is every pair of the same (station, lead) with
target <= I - 1 (yesterday's maximum is in the overnight CLI) and target >=
I - 90. Every fitted quantity (bias, width, recency weights' inputs, blend
weight, spread regression, t degrees of freedom, market blend weight) is
computed from that set and nothing else. A candidate whose own past residuals
are needed (recency) stores each pair's prediction when that pair is reached,
so those residuals were themselves produced causally.

Incumbent = production: MEX 12Z daily max + trailing-90-day mean error, Normal
with the trailing sd, continuity correction at the integer boundary.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy import stats

from src.weather.calibration import reliability_bins, reliability_slope
from src.weather.stations import STATIONS

HERE = pathlib.Path(__file__).parent
CACHE = HERE / "cache"
OUT = HERE / "results"
LEADS = (1, 2, 3)
WINDOW = 90
MIN_HISTORY = 60                 # production's per-cell sample floor
MIN_SIGMA = 0.5
LADDER = (-6, -4, -2, 0, 2, 4, 6)          # production strike ladder
BUCKETS = (-6, -4, -2, 0, 2, 4)            # 2F buckets [b, b+1] around the forecast

# Recency schemes: weights oldest -> newest over the last k calendar days.
RECENCY = {
    "REC5 10/10/15/25/40": [10, 10, 15, 25, 40],
    "REC3 20/30/50": [20, 30, 50],
    "REC7 5/5/10/10/15/25/30": [5, 5, 10, 10, 15, 25, 30],
    "REC5 equal": [1, 1, 1, 1, 1],
    "REC7 equal": [1] * 7,
}
T_DFS = (3, 4, 6, 10, 20, 50)


# --------------------------------------------------------------------- data

@dataclass
class Pair:
    station: str
    lead: int
    target: dt.date
    mex: float
    nbe: Optional[float]
    xnd: Optional[float]
    obs: float
    pred: Dict[str, Tuple[float, float]] = field(default_factory=dict)  # stored (mu, sd)

    @property
    def issued(self) -> dt.date:
        return self.target - dt.timedelta(days=self.lead)


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if -80 <= v <= 140 else None


def load_forecasts(model: str, column: str) -> Dict[Tuple[str, int, dt.date], Tuple[float, Optional[float]]]:
    out = {}
    for path in sorted((CACHE / "iem").glob(f"{model}_*.csv")):
        if path.stat().st_size == 0:
            continue                               # a recorded gap
        with path.open() as f:
            for r in csv.DictReader(f):
                if r["ftime"][11:13] != "00":
                    continue                       # 00Z rows carry the daytime max
                value = _num(r.get(column))
                if value is None:
                    continue
                # Where a run date has both 12Z and 13Z (NBE's cycle change),
                # the later row simply overwrites; they are the same issue time.
                run = dt.date.fromisoformat(r["runtime"][:10])
                target = dt.date.fromisoformat(r["ftime"][:10]) - dt.timedelta(days=1)
                lead = (target - run).days
                if lead in LEADS:
                    xnd = _num(r.get("xnd")) if model == "NBE" else None
                    out[(r["station"], lead, target)] = (value, xnd)
    return out


def load_pairs() -> Dict[Tuple[str, int], List[Pair]]:
    mex = load_forecasts("MEX", "n_x")
    nbe = load_forecasts("NBE", "txn")
    cells: Dict[Tuple[str, int], List[Pair]] = defaultdict(list)
    for st in STATIONS.values():
        truth = {dt.date.fromisoformat(k): v for k, v in
                 json.loads((CACHE / "truth" / f"{st.mos_station}.json").read_text()).items()}
        for (station, lead, target), (f, _) in mex.items():
            if station != st.mos_station or target not in truth:
                continue
            n = nbe.get((station, lead, target))
            cells[(station, lead)].append(Pair(
                station, lead, target, f, n[0] if n else None, n[1] if n else None,
                truth[target]))
    for v in cells.values():
        v.sort(key=lambda p: p.target)
    return cells


# --------------------------------------------------------------- candidates

class Dist:
    """A predictive distribution for the integer daily max."""

    def __init__(self, mu: float, sd: float, df: Optional[float] = None):
        self.mu, self.sd, self.df = mu, max(sd, MIN_SIGMA), df

    def cdf(self, x: float) -> float:
        z = (x - self.mu) / self.sd
        if self.df is None:
            return float(stats.norm.cdf(z))
        scale = math.sqrt((self.df - 2) / self.df)        # sd-matched t
        return float(stats.t.cdf(z / scale, self.df))

    def above(self, strike: float) -> float:              # P(obs > strike), integer obs
        return 1.0 - self.cdf(strike + 0.5)

    def between(self, low: float, high: float) -> float:  # P(low <= obs <= high)
        return max(0.0, self.cdf(high + 0.5) - self.cdf(low - 0.5))


def _mean_sd(xs):
    a = np.asarray(xs, float)
    return float(a.mean()), float(a.std(ddof=1)) if len(a) > 1 else MIN_SIGMA


def predict_all(p: Pair, hist: List[Pair]) -> Dict[str, Dist]:
    """Every candidate's distribution for pair p, from `hist` only."""
    d: Dict[str, Dist] = {}
    e_mex = [h.obs - h.mex for h in hist]
    b_inc, s_inc = _mean_sd(e_mex)
    d["INC  MEX+bias90 (production)"] = Dist(p.mex + b_inc, s_inc)
    p.pred["INC"] = (p.mex + b_inc, s_inc)

    # (1) recency-weighted bias over the last k calendar days before issue.
    by_day = {h.target: h.obs - h.mex for h in hist}
    for name, w in RECENCY.items():
        k = len(w)
        days = [p.issued - dt.timedelta(days=k - i) for i in range(k)]   # oldest -> newest
        got = [(wi, by_day[day]) for wi, day in zip(w, days) if day in by_day]
        bias = (sum(wi * e for wi, e in got) / sum(wi for wi, _ in got)) if got else b_inc
        mu = p.mex + bias
        resid = [h.obs - h.pred[name][0] for h in hist if name in h.pred]
        sd = _mean_sd(resid)[1] if len(resid) >= 30 else s_inc
        d[name] = Dist(mu, sd)
        p.pred[name] = (mu, sd)

    # (4) fatter tails: Student-t on the incumbent's mean and sd; df by
    # trailing log-likelihood.
    z = np.asarray([(e - b_inc) / max(s_inc, MIN_SIGMA) for e in e_mex])
    best = max(T_DFS, key=lambda df: float(np.sum(
        stats.t.logpdf(z / math.sqrt((df - 2) / df), df) - math.log(math.sqrt((df - 2) / df)))))
    normal_ll = float(np.sum(stats.norm.logpdf(z)))
    t_ll = float(np.sum(stats.t.logpdf(z / math.sqrt((best - 2) / best), best)
                        - math.log(math.sqrt((best - 2) / best))))
    d["TAILS Student-t (df fit)"] = Dist(p.mex + b_inc, s_inc, best if t_ll > normal_ll else None)

    # (2) second source, (3) spread: only where NBE exists for p and the window.
    nb_hist = [h for h in hist if h.nbe is not None and h.xnd is not None]
    if p.nbe is not None and p.xnd is not None and len(nb_hist) >= MIN_HISTORY:
        b_nbe, s_nbe = _mean_sd([h.obs - h.nbe for h in nb_hist])
        d["SRC2 NBE alone"] = Dist(p.nbe + b_nbe, s_nbe)

        b_m = float(np.mean([h.obs - h.mex for h in nb_hist]))
        m = np.asarray([h.mex + b_m for h in nb_hist])
        n = np.asarray([h.nbe + b_nbe for h in nb_hist])
        y = np.asarray([h.obs for h in nb_hist])
        diff = m - n
        w = float(np.clip(np.dot(y - n, diff) / np.dot(diff, diff), 0, 1)) if np.dot(diff, diff) > 0 else 0.5
        blend_hist = w * m + (1 - w) * n
        s_bl = float(np.std(y - blend_hist, ddof=1))
        mu_bl = w * (p.mex + b_m) + (1 - w) * (p.nbe + b_nbe)
        d["SRC2 MEX+NBE blend"] = Dist(mu_bl, s_bl)

        def spread_sd(resid: np.ndarray, x: np.ndarray, x_now: float, fallback: float) -> float:
            # var_t = a + b * xnd^2 by least squares on squared residuals, b >= 0.
            A = np.vstack([np.ones_like(x), x ** 2]).T
            coef, *_ = np.linalg.lstsq(A, resid ** 2, rcond=None)
            a, b = float(coef[0]), max(float(coef[1]), 0.0)
            var = a + b * x_now ** 2
            return math.sqrt(var) if var > MIN_SIGMA ** 2 else fallback

        xs = np.asarray([h.xnd for h in nb_hist])
        r_inc = y - (np.asarray([h.mex for h in nb_hist]) + b_inc)
        d["SPREAD NBE xnd sd, MEX mean"] = Dist(p.mex + b_inc, spread_sd(r_inc, xs, p.xnd, s_inc))
        d["SPREAD+SRC2 blend mean, xnd sd"] = Dist(mu_bl, spread_sd(y - blend_hist, xs, p.xnd, s_bl))
    return d


# ------------------------------------------------------------------ scoring

@dataclass
class Score:
    ladder_p: List[float] = field(default_factory=list)
    ladder_o: List[int] = field(default_factory=list)
    bucket_p: List[float] = field(default_factory=list)
    bucket_o: List[int] = field(default_factory=list)
    clim_p: List[float] = field(default_factory=list)       # pooled base rate (production's)
    day_ladder: Dict[dt.date, float] = field(default_factory=lambda: defaultdict(float))
    day_bucket: Dict[dt.date, float] = field(default_factory=lambda: defaultdict(float))
    crps_like_logs: List[float] = field(default_factory=list)


def information_set(pairs: List[Pair], i: int) -> List[Pair]:
    """What pairs[i]'s forecaster knew when it was issued: same cell, target in
    [issued - WINDOW, issued - 1]. The one place lookahead could enter."""
    p = pairs[i]
    lo = p.issued - dt.timedelta(days=WINDOW)
    hi = p.issued - dt.timedelta(days=1)
    return [h for h in pairs[:i] if lo <= h.target <= hi]


def brier(p, o):
    p, o = np.asarray(p), np.asarray(o)
    return float(np.mean((p - o) ** 2)) if len(p) else float("nan")


def run():
    cells = load_pairs()
    scores: Dict[Tuple[str, int], Dict[str, Score]] = {}
    lead1_dists: Dict[Tuple[str, dt.date], Dict[str, Dist]] = {}
    for (station, lead), pairs in sorted(cells.items()):
        per = defaultdict(Score)
        for i, p in enumerate(pairs):
            hist = information_set(pairs, i)
            if len(hist) < MIN_HISTORY:
                if len(hist) >= 2:
                    predict_all(p, hist)    # stores recency predictions for later residuals
                continue
            dists = predict_all(p, hist)
            # Common evaluation set: only pairs where every candidate exists.
            if not any(k.startswith("SRC2") for k in dists):
                continue
            if lead == 1:
                lead1_dists[(station, p.target)] = dists
            base = round(p.mex)
            clim = np.mean([1 if h.obs > round(h.mex) + o else 0 for h in hist for o in LADDER])
            for name, dist in dists.items():
                s = per[name]
                for o in LADDER:
                    k = base + o
                    pr, out = dist.above(k), int(p.obs > k)
                    s.ladder_p.append(pr); s.ladder_o.append(out); s.clim_p.append(clim)
                    s.day_ladder[p.target] += (pr - out) ** 2
                for o in BUCKETS:
                    lo_b = base + o
                    pr, out = dist.between(lo_b, lo_b + 1), int(lo_b <= p.obs <= lo_b + 1)
                    s.bucket_p.append(pr); s.bucket_o.append(out)
                    s.day_bucket[p.target] += (pr - out) ** 2
                s.crps_like_logs.append(math.log(max(dist.between(p.obs, p.obs), 1e-6)))
        scores[(station, lead)] = per
    return cells, scores, lead1_dists


# ----------------------------------------------------------------- bootstrap

def block_bootstrap(diff: Dict[dt.date, float], count: Dict[dt.date, int],
                    reps=2000, block=7, seed=7) -> Tuple[float, float, float]:
    """Brier difference per event (candidate - incumbent) with a 95% CI from
    a bootstrap over 7-day blocks of dates (weather errors are autocorrelated
    across days and across stations on the same day, so days are the unit)."""
    rng = random.Random(seed)
    days = sorted(diff)
    blocks = [days[i:i + block] for i in range(0, len(days), block)]
    point = sum(diff.values()) / sum(count.values())
    stats_ = []
    for _ in range(reps):
        d = c = 0.0
        for _ in blocks:
            for day in rng.choice(blocks):
                d += diff[day]; c += count[day]
        stats_.append(d / c)
    stats_.sort()
    return point, stats_[int(0.025 * reps)], stats_[int(0.975 * reps)]


# ------------------------------------------------------------------ market

def _mid(candles) -> Optional[Tuple[float, float]]:
    """(mid, spread) from the hour ending 17:00 UTC, else the 18:00 one."""
    for c in sorted(candles or [], key=lambda c: c["end_period_ts"]):
        if dt.datetime.fromtimestamp(c["end_period_ts"], dt.timezone.utc).hour not in (17, 18):
            continue
        bid, ask = c["yes_bid"].get("close"), c["yes_ask"].get("close")
        if bid is None or ask is None:
            continue
        bid, ask = float(bid), float(ask)
        if ask <= 0 or bid >= 1 or ask < bid:
            continue
        return (bid + ask) / 2, ask - bid
    return None


def market_events(lead1_dists, cells):
    """Lead-1 events on Kalshi's real strikes: (date, station, ticker, outcome,
    p_market, spread, {candidate: p_model}). Outcome is Kalshi's own result;
    GHCN agreement is reported as an alignment check."""
    from research.weather_assessment.fetch import _target_date

    obs = {(p.station, p.target): p.obs for (st, lead), ps in cells.items() if lead == 1 for p in ps}
    events, agree, checked = [], 0, 0
    for series, st in STATIONS.items():
        mp = CACHE / "kalshi" / f"markets_{series}.json"
        cp = CACHE / "kalshi" / f"candles_{series}.json"
        if not (mp.exists() and cp.exists()):
            continue
        candles = json.loads(cp.read_text())
        for m in json.loads(mp.read_text()):
            target = _target_date(m["event_ticker"])
            key = (st.mos_station, target)
            # Same day sample for every city (fetch.SAMPLE_EVERY_DAYS), so no
            # city is over-weighted by prices fetched before sampling began.
            if target is None or target.toordinal() % 4:
                continue
            if key not in lead1_dists or m["ticker"] not in candles:
                continue
            mid = _mid(candles[m["ticker"]])
            if mid is None:
                continue
            kind, lo, hi = m["strike_type"], m.get("floor_strike"), m.get("cap_strike")
            outcome = 1 if m["result"] == "yes" else 0
            if kind == "greater":
                probs = {k: d.above(lo) for k, d in lead1_dists[key].items()}
                implied = obs.get(key, None) is not None and int(obs[key] > lo)
            elif kind == "less":
                probs = {k: d.cdf(hi - 0.5) for k, d in lead1_dists[key].items()}
                implied = obs.get(key, None) is not None and int(obs[key] < hi)
            elif kind == "between":
                probs = {k: d.between(lo, hi) for k, d in lead1_dists[key].items()}
                implied = obs.get(key, None) is not None and int(lo <= obs[key] <= hi)
            else:
                continue
            if key in obs:
                checked += 1
                agree += int(implied == outcome)
            events.append((target, st.mos_station, m["ticker"], outcome, mid[0], mid[1], probs))
    return events, (agree, checked)


def market_blend(events, names):
    """For each candidate: model alone, market alone, and model blended with
    the market at a weight fitted on the trailing 90 days of events that had
    settled before the decision day (all stations pooled).

    Brier of w*model + (1-w)*market is quadratic in w:
        sum((k - o) + w (m - k))^2 = A + 2wB + w^2 C
    so the best weight on the window is -B/C, clipped to [0, 1]. Exact, and
    computed from per-day sums instead of a grid search over every event.
    """
    events = sorted(events, key=lambda e: e[0])
    days = sorted({e[0] for e in events})
    out = {}
    for name in names:
        per_day = {d: [0.0, 0.0, 0, ] for d in days}      # B, C, n
        for e in events:
            m, k, o = e[6][name], e[4], e[3]
            per_day[e[0]][0] += (k - o) * (m - k)
            per_day[e[0]][1] += (m - k) ** 2
            per_day[e[0]][2] += 1
        rows = []
        for e in events:
            day = e[0]
            # Lead-1 decisions happen the day before the target; outcomes for
            # targets up to day-2 are settled by then.
            window = [per_day[d] for d in days
                      if day - dt.timedelta(days=91) <= d <= day - dt.timedelta(days=2)]
            n = sum(x[2] for x in window)
            if n < 300:
                continue
            b, c = sum(x[0] for x in window), sum(x[1] for x in window)
            w = min(max(-b / c, 0.0), 1.0) if c > 0 else 0.5
            m, k = e[6][name], e[4]
            rows.append((e[3], m, k, w * m + (1 - w) * k, w, day))
        if rows:
            o = [r[0] for r in rows]
            out[name] = {
                "n": len(rows),
                "days": len({r[5] for r in rows}),
                "model": brier([r[1] for r in rows], o),
                "market": brier([r[2] for r in rows], o),
                "blend": brier([r[3] for r in rows], o),
                "w_model_mean": float(np.mean([r[4] for r in rows])),
                "rows": rows,
            }
    return out


# ------------------------------------------------------------------ report

def report(cells, scores, lead1_dists):
    OUT.mkdir(exist_ok=True)
    names = sorted({n for per in scores.values() for n in per})
    inc = "INC  MEX+bias90 (production)"

    # pooled daily diffs for the bootstrap
    def pooled(name, kind):
        diff, count = defaultdict(float), defaultdict(int)
        per_day_events = len(LADDER) if kind == "ladder" else len(BUCKETS)
        for per in scores.values():
            if name not in per or inc not in per:
                continue
            a = per[name].day_ladder if kind == "ladder" else per[name].day_bucket
            b = per[inc].day_ladder if kind == "ladder" else per[inc].day_bucket
            for day in a:
                diff[day] += a[day] - b[day]
                count[day] += per_day_events
        return diff, count

    lines = ["# Weather candidates: walk-forward results", "",
             f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%MZ}. "
             f"Leads {LADDER and LEADS}; {len(STATIONS)} stations; window {WINDOW} days; "
             f"history floor {MIN_HISTORY}.", ""]
    n_pairs = sum(len(per[inc].ladder_o) // len(LADDER) for per in scores.values() if inc in per)
    lines.append(f"Scored station-days (common set, all candidates present): {n_pairs:,}")
    lines += ["", "## Overall (all stations, leads 1-3)", "",
              "| candidate | ladder Brier | vs incumbent (95% CI) | bucket Brier | vs incumbent (95% CI) "
              "| BSS vs climatology | reliability slope | mean log score |",
              "|---|---|---|---|---|---|---|---|"]
    overall = {}
    for name in names:
        lp = [x for per in scores.values() if name in per for x in per[name].ladder_p]
        lo = [x for per in scores.values() if name in per for x in per[name].ladder_o]
        cp = [x for per in scores.values() if name in per for x in per[name].clim_p]
        bp = [x for per in scores.values() if name in per for x in per[name].bucket_p]
        bo = [x for per in scores.values() if name in per for x in per[name].bucket_o]
        logs = [x for per in scores.values() if name in per for x in per[name].crps_like_logs]
        bs, bb = brier(lp, lo), brier(bp, bo)
        bss = 1 - bs / brier(cp, lo)
        slope = reliability_slope(reliability_bins(lp, lo))
        ld, ll, lh = block_bootstrap(*pooled(name, "ladder"))
        bd, bl, bh = block_bootstrap(*pooled(name, "bucket"))
        base_l = brier([x for per in scores.values() for x in per[inc].ladder_p],
                       [x for per in scores.values() for x in per[inc].ladder_o])
        base_b = brier([x for per in scores.values() for x in per[inc].bucket_p],
                       [x for per in scores.values() for x in per[inc].bucket_o])
        overall[name] = dict(bs=bs, bb=bb, ld=ld, ll=ll, lh=lh, bd=bd, bl=bl, bh=bh, bss=bss,
                             slope=slope, log=float(np.mean(logs)))
        rel = lambda d, base: f"{100 * d / base:+.1f}%"
        lines.append(
            f"| {name} | {bs:.4f} | {rel(ld, base_l)} ({rel(ll, base_l)} to {rel(lh, base_l)}) "
            f"| {bb:.4f} | {rel(bd, base_b)} ({rel(bl, base_b)} to {rel(bh, base_b)}) "
            f"| {bss:.3f} | {slope:.2f} | {np.mean(logs):.3f} |")

    # per lead
    lines += ["", "## By lead: ladder Brier change vs incumbent", "",
              "| candidate | " + " | ".join(f"L{l}" for l in LEADS) + " |",
              "|---|" + "---|" * len(LEADS)]
    for name in names:
        row = []
        for lead in LEADS:
            a = [x for (st, l), per in scores.items() if l == lead and name in per for x in per[name].ladder_p]
            o = [x for (st, l), per in scores.items() if l == lead and name in per for x in per[name].ladder_o]
            b = [x for (st, l), per in scores.items() if l == lead for x in per[inc].ladder_p]
            row.append(f"{100 * (brier(a, o) / brier(b, o) - 1):+.1f}%")
        lines.append(f"| {name} | " + " | ".join(row) + " |")

    # per cell CSV + win counts
    with (OUT / "cells.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "lead", "candidate", "days", "ladder_brier", "bss_vs_climatology",
                    "brier_change_vs_incumbent_pct", "reliability_slope", "bucket_brier",
                    "bucket_change_vs_incumbent_pct"])
        wins = defaultdict(lambda: [0, 0])
        for (st, lead), per in sorted(scores.items()):
            if inc not in per:
                continue
            base, bbase = brier(per[inc].ladder_p, per[inc].ladder_o), brier(per[inc].bucket_p, per[inc].bucket_o)
            for name, s in sorted(per.items()):
                bs = brier(s.ladder_p, s.ladder_o)
                bb = brier(s.bucket_p, s.bucket_o)
                ch = 100 * (bs / base - 1)
                w.writerow([st, lead, name, len(s.ladder_o) // len(LADDER), f"{bs:.4f}",
                            f"{1 - bs / brier(s.clim_p, s.ladder_o):.3f}", f"{ch:+.1f}",
                            f"{reliability_slope(reliability_bins(s.ladder_p, s.ladder_o)):.2f}",
                            f"{bb:.4f}", f"{100 * (bb / bbase - 1):+.1f}"])
                wins[name][0] += int(bs < base)
                wins[name][1] += 1
    lines += ["", "## Cells (station x lead) where the candidate beats the incumbent on ladder Brier", ""]
    for name in names:
        lines.append(f"- {name}: {wins[name][0]} of {wins[name][1]}")

    # market
    events, (agree, checked) = market_events(lead1_dists, cells)
    lines += ["", "## Option 5: shrink toward the market (lead 1, Kalshi's real strikes)", "",
              f"Events with a usable bid/ask mid at 17:00 UTC the day before: {len(events):,}. "
              f"Kalshi result vs GHCN-implied outcome agree on {agree:,} of {checked:,}.", ""]
    mb = market_blend(events, names)
    with (OUT / "market_events.csv").open("w", newline="") as f:
        w = csv.writer(f)
        cols = sorted(names)
        w.writerow(["target", "station", "ticker", "outcome", "market_mid", "spread"] + cols)
        for e in sorted(events, key=lambda e: (e[0], e[2])):
            w.writerow([e[0], e[1], e[2], e[3], f"{e[4]:.4f}", f"{e[5]:.4f}"]
                       + [f"{e[6][c]:.4f}" for c in cols])
    if mb:
        lines += ["| model | events | days | model Brier | market Brier | blend Brier | mean weight on model |",
                  "|---|---|---|---|---|---|---|"]
        for name, r in mb.items():
            lines.append(f"| {name} | {r['n']:,} | {r['days']} | {r['model']:.4f} | {r['market']:.4f} "
                         f"| {r['blend']:.4f} | {r['w_model_mean']:.2f} |")
        # bootstrap the blend vs model for the incumbent
        if inc in mb:
            diff, count = defaultdict(float), defaultdict(int)
            for o, pm, pk, pb, w_, day in mb[inc]["rows"]:
                diff[day] += (pb - o) ** 2 - (pm - o) ** 2
                count[day] += 1
            d, l, h = block_bootstrap(diff, count)
            lines.append(f"\nIncumbent blended with market vs incumbent alone: "
                         f"{100 * d / mb[inc]['model']:+.1f}% Brier "
                         f"(95% CI {100 * l / mb[inc]['model']:+.1f}% to {100 * h / mb[inc]['model']:+.1f}%).")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return overall, mb


if __name__ == "__main__":
    cells, scores, lead1 = run()
    report(cells, scores, lead1)
