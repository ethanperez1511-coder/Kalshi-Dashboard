# Weather model candidates: walk-forward assessment (2026-10-15)

Read-only research. Nothing in `src/` changed, no database was touched, live
mode was not involved. Results: [results/summary.md](results/summary.md),
per (station, lead): [results/cells.csv](results/cells.csv).

    python -m research.weather_assessment.fetch iem|truth|kalshi   # cached
    python -m research.weather_assessment.harness                  # ~6 min

## Method
- **Incumbent = production:** MEX 12Z daily max + trailing-90-day mean
  error, Normal with the trailing sd, continuity correction.
- **No lookahead:** a forecast for day T at lead L is issued on T-L and may
  use only outcomes with target <= T-L-1, within 90 days
  (`harness.information_set`).
  - `tests/test_weather_assessment_no_lookahead.py` plants future outcomes
    as +30F errors. It fails when the window is widened to the repo's
    `target < T`, and passes as written.
- **Period:** forecasts July 2024 - October 2026, 19 stations, leads 1-3.
  After the 60-pair history floor: 43,473 scored station-days, each scored
  on the production strike ladder (+/-6F) and on 2F buckets.
- **Uncertainty:** 95% intervals from a bootstrap over 7-day blocks of dates.
  Weather errors correlate across days, and across stations on the same day.
- **Market test (option 5):** lead 1 only, because Kalshi lists weather
  markets about a day ahead. It uses Kalshi's real strikes and results, with
  the bid/ask mid at 17:00 UTC the day before. Every 4th target day from
  2025-10 to 2026-10, all 19 cities (the historical endpoint rate-limits at
  ~1-2 requests/s). 6,717 events over 79 days. Kalshi's result agreed with
  the GHCN observation on 6,717 of 6,717.

## Results (ladder Brier vs incumbent; negative = better)

| # | Candidate | Ladder Brier | 95% CI | Buckets | Cells better (of 57) |
|---|---|---|---|---|---|
| 2 | MEX + NBE blend (second source) | **-12.9%** | -14.3 to -11.6 | -3.4% | 57 |
| 2+3 | blend + NBE spread (xnd) for the sd | **-13.8%** | -15.2 to -12.5 | -4.1% | 57 |
| 2 | NBE alone | -10.6% | -12.5 to -8.8 | -2.8% | 55 |
| 3 | spread alone (MEX mean, sd from xnd) | -0.8% | -1.0 to -0.6 | -0.4% | 54 |
| 4 | fatter tails (Student-t, df fit) | -0.2% | -0.4 to -0.1 | -0.2% | 44 |
| 1 | recency bias, 7-day equal | +4.5% | +3.2 to +5.8 | +0.9% | 6 |
| 1 | recency bias, 5-day 10/10/15/25/40 | +8.7% | +7.3 to +10.1 | +1.8% | 0 |
| 1 | recency bias, 3-day 20/30/50 | +13.0% | +11.5 to +14.4 | +2.5% | 0 |

**Reliability:** every candidate in every cell has a slope between 0.85
and 1.04 (bar 0.80-1.25), and Brier skill vs climatology above 0.05.
- Incumbent BSS 0.50; blend + spread 0.57.
- The gains are sharpness, not a calibration repair.

**Recency (1) hurts, for a measurable reason.** Forecast errors barely
persist at the lags a forecaster can actually use.
- Median autocorrelation of MEX error is 0.16 at 2 days and 0.10 at 5 days.
- A 3-7 day mean therefore measures noise, applied at full strength.
- The most a shrunk version could recover is about rho^2, roughly 2% of
  error variance.

**Market (5), lead 1, same events:**

| | Brier |
|---|---|
| incumbent | 0.1325 |
| blend + spread | 0.1244 |
| market mid | 0.1153 |
| incumbent shrunk to market | 0.1153 (fitted weight on the model 0.08) |

- **The market beats every model.**
- Where model and market differ by 10 points or more, the market is closer
  to the outcome 57-59% of the time.
- Taking those trades at the touch earns -0.4c ± 1.6c per contract
  (incumbent) and +0.6c ± 1.9c (blend + spread), before fees.
- Those intervals are understated: trades on one day and city are
  correlated.

## Recommendation
1. **Build first: NBE as a second source, blended with MEX, with xnd driving
   the sd.**
   - Largest gain (-13.8% ladder Brier), better in all 57 cells, with
     reliability unchanged.
   - Same archive and pipeline as MEX (IEM `mos.py`, `model=NBE`, columns
     `txn` and `xnd`), and public domain.
   - It enters as a challenger through `promotion.py`'s challenger bar, not
     as a swap.
2. **Do not build recency weighting** in any variant tested.
3. **Fatter tails:** not worth the complexity (-0.2%).
4. **Treat the market result as a gate, not a feature.**
   - At lead 1 the market is the better forecaster, and disagreement-driven
     trades were break-even before fees in this sample.
   - Before any live phase: the trading model should beat the market's
     Brier on the events it would trade, measured the same way. That is the
     evidence for edge; win rate is not.

## Data sources and terms (checked 2026-10-15; not legal advice)

**IEM** (mesonet.agron.iastate.edu)
- "public domain and may be used freely by anyone for any lawful purpose";
  commercial use is explicitly allowed (/api/).
- Attribution is appreciated.
- No published rate limit, but it does answer 429 under bursts, so fetches
  are paced with backoff.

**NWS MOS (MEX) and NBM (NBE)**
- Public domain (weather.gov/disclaimer).
- Don't imply NWS endorsement.
- Retry no faster than once a minute during outages.

**NCEI GHCN-Daily**
- Free, no restriction stated; citation requested (Menne et al. 2012).

**Kalshi API** (Developer Agreement v1.1)
- Use only to facilitate your own trading on Kalshi.
- Storing data for that purpose is allowed.
- **No sharing with third parties without written consent.** This repo is
  public, so raw Kalshi data (cache/, results/market_events.csv) is
  git-ignored. Only aggregate scores are committed.
- Exponential backoff is required.
- Kalshi may audit use.
- The website's separate Data Terms bar scraping and ML use. Everything
  here came through the API.

**Alternatives, if a third source is ever wanted**
- ECMWF open data: CC-BY-4.0, 0.25 deg. An archive back to 2023 exists on
  the AWS mirror.
- NOAA GEFS on AWS: open, attribution requested, back to 2017.
- Open-Meteo: the free tier is non-commercial, so this system would need a
  paid plan.
