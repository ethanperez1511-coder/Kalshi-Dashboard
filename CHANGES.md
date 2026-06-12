# Changes: System Improvements

All changes keep the system in PAPER mode. Live trading is not enabled.

## 1. De-Vig Sportsbook Odds [TESTED]

**Files**: `src/modeling/odds_api.py`

Sportsbook implied probabilities include vigorish (outcomes sum to >100%). Now:
- Each bookmaker's outcomes are de-vigged independently (normalized to sum to 1)
- De-vigged probabilities are averaged across bookmakers
- Applied to h2h, totals, and spreads extraction

New functions: `devig_two_way()`, `devig_book_then_average()`

Tests: `tests/test_system_improvements.py::TestDeVig` (6 tests)

## 2. Parlay Leg Correlation

**Files**: `src/modeling/models/sports_odds.py`, `src/trading_config.py`

Same-game parlays (legs sharing a game event) violate the independence assumption.

| Config Flag | Default | Effect |
|---|---|---|
| `SKIP_SAME_GAME_PARLAYS` | `true` | Exclude same-game parlays from trading |

When a same-game parlay is detected, it is logged and skipped. A clearly-marked hook (`TODO: correlation_adjustment`) is left for a future correlation model.

## 3. Separate Real-Edge from Price-Derived Models

**Files**: `src/modeling/base.py`, `src/modeling/models/sports_odds.py`, `src/modeling/models/finance.py`, `src/ev/scorer.py`, `src/ev/filter.py`, `src/trading_config.py`

Models are now tagged as `INDEPENDENT` (SportsOddsModel) or `PRICE_DERIVED` (Finance, Sports, Consensus).

| Config Flag | Default | Effect |
|---|---|---|
| `TRADE_PRICE_DERIVED_MODELS` | `false` | Skip price-derived models (they have no real edge) |
| `PRICE_DERIVED_MIN_EDGE` | `0.10` | Higher edge threshold for price-derived models (when enabled) |
| `ENABLE_KEYWORD_PRIORS` | `false` | Gate FinanceModel keyword priors ("recession"→0.20 etc.) |

## 4. Realistic Fills + Tighter Spread

**Files**: `src/trading_config.py`, `src/ev/calculator.py`, `src/ev/scorer.py`, `src/trading/engine.py`

| Config Flag | Default | Effect |
|---|---|---|
| `MAX_SPREAD_CENTS` | `3` | Maximum bid-ask spread (was 15) |

EV calculation and paper-trade simulator now account for crossing the spread:
- **Taker**: buys at ask, sells at bid (never fills at mid)
- **Maker**: posts inside spread (bid+1 for buys)

The paper fill simulator uses the same logic.

## 5. Maker Orders

**Files**: `src/trading_config.py`, `src/ev/calculator.py`, `src/trading/engine.py`

| Config Flag | Default | Effect |
|---|---|---|
| `ORDER_TYPE` | `"maker"` | `"maker"` or `"taker"` |
| `REQUOTE_SECONDS` | `30` | Cancel/requote unfilled maker orders after N seconds |

Maker orders pay 0 fee and capture spread. Taker orders cross the spread and pay the Kalshi fee. Both paths are available.

## 6. Real Fee Formula [TESTED]

**Files**: `src/ev/calculator.py`

Replaced flat 1c/dollar fee with Kalshi's actual formula:
```
fee_per_contract = ceil_to_cent(0.07 * price * (1 - price))
```
- Maker fee = 0 on standard markets
- Legacy `fee_rate` parameter still works for backwards compat

Tests: `tests/test_system_improvements.py::TestKalshiFee` (11 tests)

## 7. Calibration Sample Guard

**Files**: `src/portfolio/metrics.py`, `src/trading_config.py`

| Config Flag | Default | Effect |
|---|---|---|
| `MIN_SETTLED_TRADES` | `100` | Don't trust calibration below this count |

The metrics API now returns `calibration_reliable: bool` and `min_settled_trades: int` so the dashboard can surface "calibration not yet reliable" state.

## 8. Kelly Shrinkage

**Files**: `src/risk/kelly.py`, `src/risk/manager.py`

Quarter-Kelly stays as base. Once `MIN_SETTLED_TRADES` is reached, a calibration-derived shrinkage multiplier adjusts the Kelly fraction:

```
multiplier = max(0.25, 1.0 - 2 * calibration_error)
effective_fraction = kelly_fraction * multiplier
```

- Perfect calibration (error=0): multiplier=1.0 (no change)
- Poor calibration (error=0.10): multiplier=0.80
- Below MIN_SETTLED_TRADES: multiplier=1.0 (quarter-Kelly unchanged)

New function: `calibration_shrinkage(calibration_error) -> float`

## 9. Backtest Lookahead Audit [TESTED]

**Files**: `src/backtest/runner.py`

- Input snapshots now filtered to `timestamp < decision_time` (no lookahead)
- Added assertion: `assert snap_dt < dec_dt` — fails if any future data leaks in
- Resolution snapshots (for determining win/loss) use post-close data

Tests: `tests/test_system_improvements.py::TestBacktestLookahead` (1 test)

## 10. Correlation-Aware Exposure

**Files**: `src/risk/limits.py`, `src/trading_config.py`

| Config Flag | Default | Effect |
|---|---|---|
| `MAX_CLUSTER_EXPOSURE` | `0.10` | Max exposure per correlated cluster (10% of bankroll) |

Positions are grouped by cluster key (extracted from the MVE event/collection ID in the market ticker). New trades that would breach the cluster cap are rejected.

## Config Flags Summary

All flags are set via environment variables (prefix `TRADING_`) or by modifying `src/trading_config.py`.

| Flag | Default | Env Var |
|---|---|---|
| `SKIP_SAME_GAME_PARLAYS` | `true` | `TRADING_SKIP_SAME_GAME_PARLAYS` |
| `TRADE_PRICE_DERIVED_MODELS` | `false` | `TRADING_TRADE_PRICE_DERIVED_MODELS` |
| `PRICE_DERIVED_MIN_EDGE` | `0.10` | `TRADING_PRICE_DERIVED_MIN_EDGE` |
| `ENABLE_KEYWORD_PRIORS` | `false` | `TRADING_ENABLE_KEYWORD_PRIORS` |
| `MAX_SPREAD_CENTS` | `3` | `TRADING_MAX_SPREAD_CENTS` |
| `ORDER_TYPE` | `"maker"` | `TRADING_ORDER_TYPE` |
| `REQUOTE_SECONDS` | `30` | `TRADING_REQUOTE_SECONDS` |
| `MIN_SETTLED_TRADES` | `100` | `TRADING_MIN_SETTLED_TRADES` |
| `MAX_CLUSTER_EXPOSURE` | `0.10` | `TRADING_MAX_CLUSTER_EXPOSURE` |

## Assumptions

1. **Cluster key extraction**: MVE ticker format `PREFIX-EVENTID-HASH` — the middle segment groups correlated legs. Non-MVE tickers use the full ID as their own cluster.
2. **Maker fee**: Assumed 0 on standard Kalshi markets (per their fee schedule).
3. **Same-game detection**: Uses Python `id()` of matched `GameOdds` objects — legs matching the same game object are flagged as correlated.
4. **Backtest resolution**: When no post-close snapshot exists, falls back to the latest snapshot available.
5. **Live trading remains disabled**: Mode stays `"paper"`, no changes to the live trading gate.

## Test Results

- 167 passed, 3 pre-existing failures in `test_ev_filter.py` (unrelated)
- 18 new tests added in `tests/test_system_improvements.py`
