# Kalshi Trading Dashboard — Plan Overview

This project is broken into 6 sequential implementation plans. Each plan produces working, testable software and builds on the previous one.

## Plan Order

1. **Project Scaffolding + Data Layer** — FastAPI server, SQLite database, Kalshi API client, offline/demo mode
2. **Probability Modeling + EV Engine** — Model plugin system, sports/finance models, EV calculation, trade filtering
3. **Risk Management + Trade Execution** — Kelly sizing, hard limits, correlation tracking, order placement, paper trading
4. **Portfolio Tracking** — Position tracking, PnL calculation, performance metrics, trade log
5. **Backtesting Engine** — Historical data pipeline, backtest runner, calibration reports
6. **Dashboard UI** — React frontend, Command Center layout, charts, settings pages

## Dependencies

```
Plan 1 (Data Layer)
  └→ Plan 2 (Modeling + EV)
       └→ Plan 3 (Risk + Execution)
            └→ Plan 4 (Portfolio)
                 └→ Plan 5 (Backtesting)
  └→ Plan 6 (Dashboard UI) — can start after Plan 1, iterates as backend features land
```
