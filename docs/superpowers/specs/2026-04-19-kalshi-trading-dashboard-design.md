# Kalshi Automated Trading Dashboard — Design Spec

## Overview

A fully automated prediction market trading system with a private web dashboard. The system identifies positive expected value (EV) opportunities on Kalshi, executes trades autonomously, and tracks performance — all accessible through a local web interface.

**Target user:** Solo trader, beginner at quantitative modeling, wants the system to handle modeling and surface actionable outputs.

**Starting capital:** $100, scaling to $1,000 once validated.

## Architecture

**Monolith.** Single Python application (FastAPI) that handles data ingestion, probability modeling, EV calculation, trade execution, risk management, and serves the React/TypeScript dashboard. One command (`python main.py`) starts everything.

```
┌─────────────────────────────────────────────────┐
│                  FastAPI Server                   │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Data      │  │ Modeling  │  │ Trading       │  │
│  │ Ingestion │→ │ & EV     │→ │ Engine        │  │
│  │           │  │ Engine    │  │ (auto-execute)│  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│       ↑              ↑              ↑             │
│  ┌──────────────────────────────────────────┐    │
│  │           SQLite Database                 │    │
│  └──────────────────────────────────────────┘    │
│       │                                           │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Risk     │  │ Portfolio │  │ Backtesting   │  │
│  │ Manager  │  │ Tracker   │  │ Engine        │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│                                                   │
│  ┌──────────────────────────────────────────┐    │
│  │  React Dashboard (served by FastAPI)      │    │
│  └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
         ↕                    ↕
    Kalshi API          External Data APIs
  (REST + WebSocket)   (sports, economics)
```

**Tech stack:**
- Backend: Python 3.12+, FastAPI, SQLite, SQLAlchemy
- Frontend: React, TypeScript, Vite, Recharts (for charts)
- Data: pandas, scipy, statsmodels for modeling

**Database:** SQLite — no server to manage, file-based, perfect for single-user.

## Data Ingestion

### Kalshi API
- REST API for market discovery, order placement, account info
- WebSocket for real-time price/orderbook updates
- Poll REST every 60s for market listings; WebSocket for live prices on tracked markets
- Request queue with backoff to respect rate limits

### Data stored
- **Markets:** market ID, title, category, close date, status, rules
- **Prices:** timestamped bid/ask/last/volume snapshots (every 10s from WebSocket)
- **Orderbook:** depth snapshots for liquidity assessment

### External data sources (pluggable per market category)
- **Sports:** Free tier APIs (ESPN, odds APIs) for scores, schedules, odds
- **Finance/Economics:** FRED API (free) for economic indicators, Fed data
- **Weather:** OpenWeatherMap or NOAA (free tiers)
- New sources added as plugins over time

### Offline/demo mode
System starts in offline mode with sample data until Kalshi API credentials are configured.

## Probability Modeling & EV Engine

### Model approach
Market-category-aware with a common interface. Each model plugin takes external data and outputs `p_model`. Simple, explainable models — no black boxes.

- **Sports:** Historical win rates, recent form, odds from other bookmakers as consensus anchor
- **Finance:** Historical base rates for economic events combined with current market consensus
- **Fallback:** Wisdom-of-crowds correction — compare Kalshi price to other prediction markets (Polymarket, PredictIt) to find pricing discrepancies

### EV calculation
```
EV = p_model * (1 - price) - (1 - p_model) * price
Net EV = EV - fees - estimated_slippage
Edge = p_model - implied_probability
```

### Model confidence
Each model outputs a confidence score (0.0–1.0) based on:
- Data freshness (how recent the external data is)
- Data completeness (are all expected inputs available?)
- Historical calibration (how well this model has performed on similar markets)

Confidence tiers:
- High (>0.7): standard edge threshold (5%)
- Medium (0.4–0.7): raised edge threshold (8%)
- Low (<0.4): raised edge threshold (12%) or skip

## Trade Filtering

ALL conditions must pass:
- Net EV > 0
- Edge > 5% (configurable)
- Market daily volume > $500
- Bid/ask spread < 5 cents
- Model confidence above minimum threshold
- Time to expiry > 1 hour

## Risk Management

### Position sizing — Fractional Kelly
- Quarter Kelly (0.25x) by default
- Formula: `size = (kelly_fraction * 0.25) * bankroll`
- At $100: typical position $0.50–$2.00

### Hard limits (non-overridable)
- Max single trade: 3% of bankroll
- Max total exposure: 25% of bankroll
- Max correlated exposure: 10% of bankroll
- Daily loss limit: 5% — system pauses 24 hours if hit
- Drawdown circuit breaker: 20% from peak — system stops and alerts

### Correlation tracking
Markets tagged by category/sub-category. Same-category markets share exposure limits.

### Paper trading enforcement
- Paper mode is the default — no real trades until explicitly enabled AND API credentials provided
- Must run at least 50 paper trades before live mode unlocks (configurable)

## Trade Execution

1. EV engine identifies qualifying opportunity
2. Risk manager checks all limits
3. If approved → place limit order (never market orders)
4. Monitor: if not filled within 5 minutes, cancel and re-evaluate
5. Log every decision, skip, and fill

### Order management
- Limit orders only — calculates optimal bid based on edge and orderbook depth
- Cancel rather than chase if orderbook moves away

## Portfolio Tracking

### Stored data
- Open positions: entry price, current price, unrealized PnL
- Closed positions: realized PnL
- Running bankroll total
- EV vs actual performance comparison

### Performance metrics
- Total return (%)
- Sharpe-like ratio
- Calibration score (p_model vs actual outcomes)
- Win rate (secondary metric)
- Max drawdown from peak

### Trade log per trade
- Market, direction, entry/exit price
- p_model, implied probability, edge, net EV at entry
- Position size and reasoning
- Outcome and PnL

## Backtesting Engine

- Runs the full pipeline (modeling + EV + risk) against historical Kalshi data
- On-demand from dashboard ("Run backtest" button)
- Configurable date range and market filters
- Results stored in SQLite

### Reports
- Total return, EV per trade (average), max drawdown
- Calibration chart (predicted vs actual frequency)
- Equity curve over backtest period
- Number of trades, win rate

### Limitations (displayed prominently)
- Historical data availability may be limited
- Backtests assume fills at historical prices
- Past performance doesn't guarantee future results

## Dashboard UI

### Style
Bloomberg Terminal aesthetic — deep navy background (#0c0f1a), colored gradient accents per section (blue for opportunities, purple for positions, cyan for activity, amber for warnings). Subtle glow effects, gradient text for key metrics, clean Inter typography.

### Layout — Command Center (single page overview)

**Top nav:** Logo, page links (Overview, Markets, Backtest, Settings), system status indicator (green dot + "System Active"), paper/live mode badge.

**KPI row (5 cards):**
- Bankroll (blue accent) — current total + % change
- Total Return (green accent) — actual vs EV expected
- Open Positions (purple accent) — count + total exposure
- Max Drawdown (amber accent) — current vs limit
- Total Trades (cyan accent) — count + win rate

**3-column main area:**
1. **Live Opportunities** — ranked by net EV. Each shows: market name, direction, price, model probability, edge %, EV, position size, execution status, category tag. Color-coded border: green (qualifying), amber (watching), gray (below threshold).
2. **Open Positions** — entry → current price, unrealized PnL, time-to-expiry progress bar. Summary footer: total exposure %, unrealized PnL.
3. **Activity Log** — chronological feed of trades (opened/closed), skips, and risk alerts. Color-coded dots: green (win), red (loss), blue (opened), gray (skipped), amber (alert).

**Bottom row (2 charts):**
1. **Equity Curve** — line chart with gradient fill, time-range toggles (7d/30d/All)
2. **Model Calibration** — scatter plot of predicted probability vs actual outcome frequency, with perfect calibration reference line

### Additional pages (via nav)
- **Markets:** Browse all Kalshi markets, filter by category, see model estimates vs market price
- **Backtest:** Configure and run backtests, view results
- **Settings:** API credentials, bankroll, risk parameters, model thresholds, paper/live toggle

## Bankroll Scaling
Starting at $100. When validated, update bankroll setting to $1,000 — all position sizes and limits adjust automatically. No code changes needed.

## Constraints
- Never claim guaranteed profits
- Always display uncertainty and assumptions
- Prefer simple, explainable models over black-box predictions
- Ignore low-quality signals even if they suggest EV
- System must survive — prioritize capital preservation over growth
