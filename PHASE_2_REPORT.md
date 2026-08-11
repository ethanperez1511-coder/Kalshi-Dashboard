# Phase 2 — Weather model

**Date:** 2026-08-11
**Tests:** 472 default + 2 live (real Kalshi and IEM calls). Zero failures.
**Mode:** paper, unchanged. Bankroll $101.35, 11/50 paper trades.

---

## Honest summary

Dispersion is **estimated from historical forecast error, not observed**. The MOS
forecast carries no uncertainty of its own, so this model cannot distinguish a
confident day from an uncertain one — it applies that cell's typical error width
to every day alike. Flow-dependent uncertainty is the known gap, and the GEFS
ensemble challenger is the planned fix, pending its probe.

The rolling refit was **adopted after a diagnosed fixed-window failure**, and
re-validated on the same unchanged bar. Details in §3.

All calibration evidence is retrospective. **Live paper performance is the
prospective confirmation**, and nothing about clearing the offline gate should be
read as more than "allowed to start proving itself".

---

## 1. What was actually blocking the model

Weather contracts were unreachable. Measured against the live API:

- `/markets`, first 3000 rows (the fetch cap): 1549 General + 1451 Sports,
  **zero** weather tickers.
- `/events` feed: 26 "Climate and Weather" markets, all long-horizon
  (supervolcanoes, 2050 warming).

Daily temperature contracts exist **only** behind an explicit `series_ticker`
query. Without Phase 2.0's series ingest the model would have had nothing to
score, and no amount of modelling work would have shown it.

Two facts read off the contracts rather than assumed:

- **Direction is per-contract, not per-city.** New York lists both `>90°` and
  `<83°` for the same day, as does Austin. A per-city rule would have mispriced
  half the book.
- **Chicago settles on MIDWAY, not O'Hare.** Nothing in the ticker says which.

**Boundary:** `floor_strike=90` with subtitle "91° or above" means YES iff
T ≥ 91 — strictly greater. Probabilities use a continuity correction at 90.5.
Without it every at-the-money strike is off by roughly half a degree of
probability mass, and the bucket ladder does not sum to 1.

`between` buckets are **56 of 84** live contracts. Each city-day is a complete
partition (`<84 · [84,85] · [86,87] · [88,89] · [90,91] · >91`), which gives the
strongest free integrity check the market structure offers:

```
 forecast     <84   84-85   86-87   88-89   90-91     >91       SUM
     82.0  0.6659  0.1755  0.1006  0.0420  0.0127  0.0033  1.000000
     87.0  0.1587  0.1755  0.2227  0.2057  0.1383  0.0993  1.000000
     93.0  0.0033  0.0127  0.0420  0.1006  0.1755  0.6659  1.000000

max |sum - 1| over sigma 1..8, forecast 70..100:  0.000e+00
```

---

## 2. Data architecture, and why each piece

| Role | Source | Why |
|---|---|---|
| Predictor | **NWS MOS (MEX) via IEM archive** | NWS's own gridpoint API has no archive, so σ cannot be fitted against it. Fitting on one product while pricing off another measures a different model's errors than the one placing trades. Predictor and production are the same product. |
| Truth | **NCEI GHCN-Daily TMAX** | The same official observation the CLI settles on, as a deep archive rather than a two-week window, in whole degrees. |
| Settlement check | NWS CLI product | `MAXIMUM 85 319 PM`, ~2 weeks retained. |

Open-Meteo is demoted to paper-only backfill: its free tier is CC-BY-4.0
**non-commercial**, which is not acceptable behind real capital.

Using a gridded reanalysis as truth would have been the quiet mistake here.
Measured: an Open-Meteo grid value ran **+1.50 °F mean, +3.3 °F max** against
the Central Park observation. Against 1-degree buckets that gap alone dominates
every modelling refinement.

**Day alignment was verified against truth, not assumed.** MOS reports maxima
and overnight minima in the same `n_x` field, distinguished only by valid time.
The 00Z value is the maximum for the local day that just ended (KPHL, run
2025-08-01 12Z vs GHCN: errors −1, −1, −3, +3 across leads 1–4 — forecast error,
not a day offset).

---

## 3. Calibration: the failure, the diagnosis, the remedy

Fit **Apr–Aug 2025**, evaluate **Sep–Dec 2025**, 7 stations × leads 1–3,
**105 held-out days per cell**, forecast-centred strikes (the honest test —
tail strikes are trivially predictable and inflate every skill number).

### Fixed window: 20 of 21 cells cleared

`KXHIGHNY` lead 3 failed on **reliability slope 1.313** against a 1.25 ceiling.
Skill was fine (0.525).

### Diagnosis before remedy — it is the window, not the model

**Degradation is not uniform.** Slope drift from lead 1 to lead 3 orders exactly
by station seasonality:

```
KXHIGHNY   +0.195 ┐                KXHIGHDEN  +0.033
KXHIGHCHI  +0.171 ├ continental    KXHIGHMIA  +0.015 ┐ marine / tropical
KXHIGHPHIL +0.096 ┘                KXHIGHLAX  −0.059 ┘
KXHIGHAUS  +0.056
```

**The sign of (fitted σ − realized σ) predicts every miss**, in both directions:

| Cell | fitted σ | realized σ | slope | reading |
|---|---|---|---|---|
| NY L3 | 4.64 | 2.72 | 1.313 | too wide → underconfident |
| PHIL L3 | 3.96 | 2.65 | 1.116 | too wide |
| MIA L3 | 1.98 | 2.05 | 0.992 | matched |
| LAX L3 | 2.38 | 4.20 | 0.880 | too narrow → overconfident |
| DEN L1 | 3.07 | 4.20 | 0.887 | too narrow |

A misspecified model degrades everywhere and in one direction. This is a stale
fit: summer errors run larger than autumn on continental stations and smaller on
marine ones, and Miami — which barely has seasons — barely moves.

### Remedy: rolling 90-day refit. Bar unchanged.

Refit on a trailing 90-day window before every evaluation day, strictly causal
(`target_date < as_of`). Same gate, same held-out pairs, same thresholds.

**All 21 cells promote.** Slopes tighten **[0.880, 1.313] → [0.889, 1.034]**.
Brier skill improves in 15 of 21. The failed cell goes 0.525 → 0.562 with slope
1.313 → **1.011**. No day was skipped for a thin window.

### Per-station, held-out only

| Station | Brier skill L1/L2/L3 | Reliability slope L1/L2/L3 | Clears |
|---|---|---|---|
| MIA | 0.720 / 0.696 / 0.670 | 1.008 / 1.013 / 1.008 | ✅ |
| NY | 0.626 / 0.587 / 0.562 | 1.032 / 1.034 / 1.011 | ✅ |
| PHIL | 0.592 / 0.557 / 0.545 | 0.955 / 0.942 / 0.959 | ✅ |
| LAX | 0.585 / 0.550 / 0.508 | 0.973 / 0.984 / 0.956 | ✅ |
| CHI | 0.566 / 0.491 / 0.468 | 0.923 / 0.927 / 0.982 | ✅ |
| AUS | 0.508 / 0.388 / 0.386 | 0.987 / 0.889 / 0.967 | ✅ |
| **DEN** | **0.401 / 0.384 / 0.333** | 0.967 / 0.998 / 1.009 | ✅ (watch) |

**Cells below the N=60 floor: none** — 105 held-out pairs each.

**Why N=60:** two parameters are fitted per cell, and the standard error of an
sd estimate is `sd/√(2(n−1))`, so n=60 puts it near 9% relative. It is also
about two months, enough that one unusual week cannot dominate a cell.

---

## 4. Enforcement at prediction time

The harness is not what trades, so every gate is enforced again in the live
model. Four independent refusals, each with its own reason so the digest can
tell them apart:

| Gate | Refuses when |
|---|---|
| terms | contract never parsed — no direction, no strike |
| cell | not promoted / below floor / **fit older than its 7-day cadence** |
| guard | live settled Brier degraded past tolerance |
| sanity | forecast disagrees with the market by more than weather ever does |

None produce a widened spread, a smaller size, or a downweighted probability.
A broken input is not a low-confidence input.

**Staleness matters specifically because the validated design refits before
pricing.** A fit from three weeks ago is not the thing that was validated, so
pricing off it is pricing off parameters nobody measured.

**Promotion is per (station, lead).** A promoted lead-1 cell does not let an
unpromoted lead-2 price, and a failing cell neither blocks its neighbours nor is
carried by them. Leads beyond 3 do not price at all — borrowing lead 3's σ for
lead 6 understates the error badly.

### The tripwire

MOS reports maxima and overnight minima in the same field. Reading a 12Z row as
a daily max puts the forecast ~20 °F under truth, and that does not present as a
bug — it presents as every contract in the city being mispriced the same way,
which is to say as the best edge the system has ever found.

Correct parsing is necessary but not sufficient: a unit error, a station
mix-up, a stale run, or a future parser refactor all produce the same signature.
So the check lives at the model layer and keys on the **disagreement itself**,
measured against the market-implied median recovered from the ladder. A tripped
station-day is unpriceable and reported. A 6 °F disagreement still trades — that
is the model earning its keep.

### Regime guard — newly built

No regime-guard machinery existed anywhere in the codebase; the only prior
"pause" was the daily-loss limit. Added here, per series: rolling Brier over the
last 20 settled paper trades, pausing the cell above 0.30 (predicting 0.5 blindly
scores 0.25, so sustained worse than that means actively misinformed). NO bets
are scored on `1 − p_model`, or a confident correct NO would read as a confident
wrong YES. Denver degrading cannot silence New York.

---

## 5. Self-archive — running from today

Every forecast is recorded daily, traded on or not, because a day not archived
cannot be reconstructed. Verified live: **56 MOS rows + 56 gridpoint rows** across
7 stations, 8 forward days.

The smoke test caught a real gap: MEX publishes twice daily, and archiving only
the 12Z run wrote **zero** MOS rows before that run posted. Both runs are now
recorded and tagged by hour, so fitting can still select a single cadence while
the archive keeps whatever was actually available.

This is what the migration off the third-party MOS archive depends on, and it is
the only route to fitting the **NWS gridpoint challenger**, which publishes no
archive at all.

---

## 6. What you need to know before Phase 3

1. **Nothing trades yet on weather.** Fits are not populated in production —
   the refit job needs to run against Neon before any cell prices. Until then
   every cell refuses on "no fit on record".
2. **Denver is the watch flag.** It cleared with the least room (0.333 at lead 3
   against a 0.05 bar) and carries a live-Brier line in the daily digest.
3. **The paper sample is still SportsOdds-only.** Polymarket remains at zero
   after the Phase 1.5 horizon ruling. Per-model and per-station trade counts are
   in the digest so the 50-trade gate cannot be satisfied by one model while
   others have no settled evidence. Enforcing a per-model minimum before live
   remains a policy decision, deliberately not implemented.
4. **GEFS challenger is pending its probe**, with acceptance criteria already
   recorded in `tasks/todo.md`: documented member counts, RMSE growing
   monotonically with lead, no silent-null failure mode, and the Python 3.11+
   constraint confirmed CI-only. If it passes it ships as a scored challenger
   that must beat the MOS baseline on the same held-out window.
