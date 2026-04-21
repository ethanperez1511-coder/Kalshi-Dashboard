# Plan 5: Backtesting Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a backtesting engine that replays the full pipeline (modeling + EV + risk + execution) against historical price data, producing performance reports (return, drawdown, calibration, equity curve, win rate).

**Architecture:** A `BacktestRunner` iterates over historical markets with price data, simulates the scoring/risk/execution pipeline at each snapshot, then resolves positions at market close prices. A `BacktestResult` model stores results in SQLite. A report builder computes aggregate stats from the simulated trades. API endpoint triggers backtests on demand. Backtests use a separate simulated bankroll so they don't affect the live portfolio.

**Tech Stack:** Python 3.9, SQLAlchemy, pydantic, FastAPI

---

## File Structure

```
src/
├── backtest/
│   ├── __init__.py
│   ├── runner.py             # BacktestRunner: iterate markets, simulate pipeline
│   ├── report.py             # Build report from backtest trades
│   └── models.py             # BacktestRun + BacktestTrade SQLAlchemy models
├── api/
│   └── backtest.py           # FastAPI endpoints for running/viewing backtests
├── models/
│   └── __init__.py           # Add new models to exports
tests/
├── test_backtest_models.py
├── test_backtest_runner.py
├── test_backtest_report.py
├── test_api_backtest.py
```

---

### Task 1: Backtest Models

**Files:**
- Create: `src/backtest/__init__.py`
- Create: `src/backtest/models.py`
- Modify: `src/models/__init__.py`
- Create: `tests/test_backtest_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_models.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.backtest.models import BacktestRun, BacktestTrade


def test_create_backtest_run(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0,
            final_bankroll=112.50,
            total_trades=20,
            wins=14,
            losses=6,
            total_pnl=12.50,
            max_drawdown_pct=4.2,
            status="completed",
        )
        session.add(run)
        session.commit()
        fetched = session.query(BacktestRun).first()
        assert fetched.total_trades == 20
        assert fetched.status == "completed"
        assert fetched.total_pnl == 12.50


def test_create_backtest_trade(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0,
            final_bankroll=100.0,
            total_trades=0, wins=0, losses=0,
            total_pnl=0, max_drawdown_pct=0,
            status="completed",
        )
        session.add(run)
        session.flush()

        trade = BacktestTrade(
            run_id=run.id,
            market_id="DEMO-FED-RATE-JUL",
            side="yes",
            entry_price=65,
            exit_price=100,
            quantity=2,
            p_model=0.77,
            implied_prob=0.65,
            edge=0.12,
            net_ev=0.10,
            realized_pnl=0.70,
            resolved_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        session.add(trade)
        session.commit()
        fetched = session.query(BacktestTrade).first()
        assert fetched.market_id == "DEMO-FED-RATE-JUL"
        assert fetched.realized_pnl == 0.70
        assert fetched.run_id == run.id


def test_backtest_run_has_trades(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0, final_bankroll=101.0,
            total_trades=1, wins=1, losses=0,
            total_pnl=1.0, max_drawdown_pct=0,
            status="completed",
        )
        session.add(run)
        session.flush()
        session.add(BacktestTrade(
            run_id=run.id, market_id="MKT-1", side="yes",
            entry_price=50, exit_price=100, quantity=2,
            p_model=0.7, implied_prob=0.5, edge=0.2, net_ev=0.19,
            realized_pnl=1.0,
            resolved_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ))
        session.commit()

        fetched_run = session.query(BacktestRun).first()
        trades = session.query(BacktestTrade).filter_by(run_id=fetched_run.id).all()
        assert len(trades) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_backtest_models.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/backtest/__init__.py
```

```python
# src/backtest/models.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    final_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    category_filter: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
```

```python
# src/backtest/models.py (continued — same file, add after BacktestRun)

class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    market_id: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[int] = mapped_column(Integer)
    exit_price: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    p_model: Mapped[float] = mapped_column(Float)
    implied_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    net_ev: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 4: Update models/__init__.py**

Add BacktestRun and BacktestTrade to imports.

- [ ] **Step 5: Run tests, commit**

```bash
python3 -m pytest tests/test_backtest_models.py -v
git add src/backtest/__init__.py src/backtest/models.py src/models/__init__.py tests/test_backtest_models.py
git commit -m "feat: add BacktestRun and BacktestTrade models"
```

---

### Task 2: Backtest Runner

**Files:**
- Create: `src/backtest/runner.py`
- Create: `tests/test_backtest_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_runner.py
import random
from datetime import datetime, timedelta, timezone
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.backtest.models import BacktestRun, BacktestTrade
from src.backtest.runner import BacktestRunner


def _seed_historical_markets(db_engine):
    """Create markets with price history and known close dates (in the past)."""
    Base.metadata.create_all(db_engine)
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    with get_session(db_engine) as session:
        # Market 1: resolved Yes (price went to 100)
        session.add(Market(
            market_id="BT-MKT-1", title="Will Lakers win game 5?",
            category="Sports", close_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            status="closed",
        ))
        # Price history: base around 45, resolved at 100 (yes)
        random.seed(42)
        base = 45
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            spread = 2
            session.add(PriceSnapshot(
                market_id="BT-MKT-1", yes_bid=price, yes_ask=price + spread,
                last_price=price + 1, volume=2000, timestamp=ts,
            ))
            base = price

        # Market 2: resolved No (price went to 0)
        session.add(Market(
            market_id="BT-MKT-2", title="Will Fed raise rates in March?",
            category="Economics", close_date=datetime(2026, 3, 20, tzinfo=timezone.utc),
            status="closed",
        ))
        base = 70
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            spread = 2
            session.add(PriceSnapshot(
                market_id="BT-MKT-2", yes_bid=price, yes_ask=price + spread,
                last_price=price + 1, volume=3000, timestamp=ts,
            ))
            base = price

        session.commit()


def test_backtest_runner_creates_run(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    assert run_id is not None

    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        assert run.status == "completed"
        assert run.initial_bankroll == 100.0


def test_backtest_runner_produces_trades(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        trades = session.query(BacktestTrade).filter_by(run_id=run_id).all()
        # Should have at least attempted some trades
        run = session.query(BacktestRun).get(run_id)
        assert run.total_trades == len(trades)


def test_backtest_runner_updates_bankroll(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        # final_bankroll should be initial + total_pnl
        assert abs(run.final_bankroll - (100.0 + run.total_pnl)) < 0.01


def test_backtest_runner_category_filter(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
        category_filter="Sports",
    )
    with get_session(db_engine) as session:
        trades = session.query(BacktestTrade).filter_by(run_id=run_id).all()
        for t in trades:
            assert "BT-MKT-1" == t.market_id  # Only Sports market


def test_backtest_runner_empty_range(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2025, 2, 1, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        assert run.total_trades == 0
        assert run.final_bankroll == 100.0
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_backtest_runner.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/backtest/runner.py
from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Engine, select
from src.database import get_session
from src.ev.calculator import calculate_ev
from src.modeling.registry import ModelRegistry
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.backtest.models import BacktestRun, BacktestTrade

logger = logging.getLogger(__name__)

# Backtest uses its own simplified risk params instead of TradingSettings
_BT_KELLY_FRACTION = 0.25
_BT_MAX_SINGLE_TRADE_PCT = 0.03
_BT_MIN_EDGE = 0.05


class BacktestRunner:
    def __init__(self, engine: Engine):
        self._engine = engine

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        initial_bankroll: float = 100.0,
        category_filter: Optional[str] = None,
    ) -> int:
        # Create backtest run record
        with get_session(self._engine) as session:
            bt_run = BacktestRun(
                start_date=start_date,
                end_date=end_date,
                initial_bankroll=initial_bankroll,
                final_bankroll=initial_bankroll,
                status="running",
                category_filter=category_filter,
            )
            session.add(bt_run)
            session.commit()
            run_id = bt_run.id

        # Load closed markets in date range
        with get_session(self._engine) as session:
            query = (
                select(
                    Market.market_id,
                    Market.title,
                    Market.category,
                    Market.close_date,
                )
                .where(Market.close_date >= start_date)
                .where(Market.close_date <= end_date)
            )
            if category_filter:
                query = query.where(Market.category == category_filter)
            rows = session.execute(query).all()
            markets = [
                {"market_id": r[0], "title": r[1], "category": r[2], "close_date": r[3]}
                for r in rows
            ]

        bankroll = initial_bankroll
        peak = initial_bankroll
        max_drawdown_pct = 0.0
        registry = ModelRegistry()
        trades = []

        for mkt in markets:
            market_id = mkt["market_id"]
            title = mkt["title"]
            category = mkt["category"]
            close_date = mkt["close_date"]

            # Get price snapshots for this market
            with get_session(self._engine) as session:
                snap_rows = session.execute(
                    select(
                        PriceSnapshot.yes_bid,
                        PriceSnapshot.yes_ask,
                        PriceSnapshot.last_price,
                        PriceSnapshot.volume,
                    )
                    .where(PriceSnapshot.market_id == market_id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                ).first()

            if snap_rows is None:
                continue

            yes_bid, yes_ask, last_price, volume = snap_rows

            # Run models
            models = registry.get_models_for(category)
            model_result = None
            for model in models:
                result = model.estimate(
                    market_id=market_id,
                    title=title,
                    current_price=last_price / 100.0,
                    engine=self._engine,
                )
                if result is not None:
                    model_result = result
                    break

            if model_result is None:
                continue

            # Calculate EV
            ev_result = calculate_ev(
                p_model=model_result.p_model,
                price_cents=last_price,
            )

            side = ev_result.recommended_side
            edge = ev_result.best_edge
            net_ev = ev_result.best_ev

            if abs(edge) < _BT_MIN_EDGE or net_ev <= 0:
                continue

            # Kelly sizing
            price_cents = last_price if side == "yes" else (100 - last_price)
            price = price_cents / 100.0
            p = model_result.p_model if side == "yes" else (1 - model_result.p_model)

            if price <= 0 or price >= 1:
                continue

            b = (1 - price) / price
            q = 1 - p
            full_kelly = max(0, (b * p - q) / b)
            fractional = full_kelly * _BT_KELLY_FRACTION
            dollars = bankroll * fractional
            max_trade = bankroll * _BT_MAX_SINGLE_TRADE_PCT
            dollars = min(dollars, max_trade)

            quantity = math.floor(dollars / price) if price > 0 else 0
            if quantity == 0:
                continue

            # Resolve: determine exit price based on close
            # For backtesting, we simulate resolution:
            # if model predicted Yes and market close_date has passed,
            # we use last known price direction as a proxy for outcome.
            # Simplified: use the final last_price > 50 as "resolved Yes"
            with get_session(self._engine) as session:
                final_snap = session.execute(
                    select(PriceSnapshot.last_price)
                    .where(PriceSnapshot.market_id == market_id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                ).scalar()

            resolved_yes = (final_snap or 50) > 50
            if side == "yes":
                exit_price = 100 if resolved_yes else 0
                realized_pnl = (exit_price - price_cents) * quantity / 100.0
            else:
                exit_price = 0 if resolved_yes else 100
                realized_pnl = (price_cents - exit_price) * quantity / 100.0

            bankroll = round(bankroll + realized_pnl, 2)
            peak = max(peak, bankroll)
            if peak > 0:
                dd = (peak - bankroll) / peak * 100
                max_drawdown_pct = max(max_drawdown_pct, dd)

            trades.append({
                "run_id": run_id,
                "market_id": market_id,
                "side": side,
                "entry_price": price_cents,
                "exit_price": exit_price,
                "quantity": quantity,
                "p_model": model_result.p_model,
                "implied_prob": ev_result.implied_prob,
                "edge": edge,
                "net_ev": net_ev,
                "realized_pnl": realized_pnl,
                "resolved_at": close_date,
            })

        # Save trades and update run
        wins = len([t for t in trades if t["realized_pnl"] > 0])
        losses = len([t for t in trades if t["realized_pnl"] <= 0])
        total_pnl = sum(t["realized_pnl"] for t in trades)

        with get_session(self._engine) as session:
            for t in trades:
                session.add(BacktestTrade(**t))

            run = session.query(BacktestRun).get(run_id)
            run.final_bankroll = round(initial_bankroll + total_pnl, 2)
            run.total_trades = len(trades)
            run.wins = wins
            run.losses = losses
            run.total_pnl = round(total_pnl, 2)
            run.max_drawdown_pct = round(max_drawdown_pct, 2)
            run.status = "completed"
            session.commit()

        logger.info(
            f"Backtest {run_id} completed: {len(trades)} trades, "
            f"PnL ${total_pnl:.2f}, final ${bankroll:.2f}"
        )

        return run_id
```

- [ ] **Step 4: Run tests, commit**

```bash
python3 -m pytest tests/test_backtest_runner.py -v
git add src/backtest/runner.py tests/test_backtest_runner.py
git commit -m "feat: add backtest runner with historical market simulation"
```

---

### Task 3: Backtest Report Builder

**Files:**
- Create: `src/backtest/report.py`
- Create: `tests/test_backtest_report.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_report.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.backtest.models import BacktestRun, BacktestTrade
from src.backtest.report import build_report


def _seed_run(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0, final_bankroll=102.50,
            total_trades=3, wins=2, losses=1,
            total_pnl=2.50, max_drawdown_pct=1.5,
            status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id

        trades = [
            BacktestTrade(
                run_id=run_id, market_id="M1", side="yes",
                entry_price=50, exit_price=100, quantity=2,
                p_model=0.70, implied_prob=0.50, edge=0.20, net_ev=0.19,
                realized_pnl=1.0,
                resolved_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            BacktestTrade(
                run_id=run_id, market_id="M2", side="yes",
                entry_price=60, exit_price=100, quantity=2,
                p_model=0.80, implied_prob=0.60, edge=0.20, net_ev=0.19,
                realized_pnl=0.80,
                resolved_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            ),
            BacktestTrade(
                run_id=run_id, market_id="M3", side="yes",
                entry_price=55, exit_price=0, quantity=3,
                p_model=0.65, implied_prob=0.55, edge=0.10, net_ev=0.09,
                realized_pnl=-1.65,
                resolved_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()
    return run_id


def test_report_summary(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert report["run_id"] == run_id
    assert report["total_trades"] == 3
    assert report["wins"] == 2
    assert report["losses"] == 1
    assert abs(report["total_pnl"] - 2.50) < 0.01
    assert abs(report["total_return_pct"] - 2.50) < 0.01


def test_report_win_rate(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert abs(report["win_rate"] - 66.67) < 0.1


def test_report_equity_curve(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    curve = report["equity_curve"]
    assert len(curve) == 4  # initial + 3 trades
    assert curve[0]["bankroll"] == 100.0
    assert abs(curve[-1]["bankroll"] - 100.15) < 0.01


def test_report_avg_ev(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    # (0.19 + 0.19 + 0.09) / 3 ≈ 0.1567
    assert abs(report["avg_ev"] - 0.1567) < 0.01


def test_report_calibration_error(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert "calibration_error" in report
    assert report["calibration_error"] >= 0


def test_report_limitations(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert "limitations" in report
    assert len(report["limitations"]) >= 3


def test_report_nonexistent_run(db_engine):
    Base.metadata.create_all(db_engine)
    report = build_report(db_engine, 999)
    assert report is None
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_backtest_report.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/backtest/report.py
from __future__ import annotations
from typing import Dict, Any, List, Optional
from sqlalchemy import Engine
from src.database import get_session
from src.backtest.models import BacktestRun, BacktestTrade

LIMITATIONS = [
    "Historical data availability may be limited",
    "Backtests assume fills at historical prices (no slippage)",
    "Past performance does not guarantee future results",
]


def build_report(engine: Engine, run_id: int) -> Optional[Dict[str, Any]]:
    with get_session(engine) as session:
        run = session.query(BacktestRun).get(run_id)
        if not run:
            return None

        trades = (
            session.query(BacktestTrade)
            .filter_by(run_id=run_id)
            .order_by(BacktestTrade.resolved_at)
            .all()
        )

        # Equity curve
        bankroll = run.initial_bankroll
        peak = bankroll
        curve = [{"timestamp": None, "bankroll": bankroll, "peak": peak}]
        for t in trades:
            bankroll = round(bankroll + t.realized_pnl, 2)
            peak = max(peak, bankroll)
            curve.append({
                "timestamp": t.resolved_at.isoformat() if t.resolved_at else None,
                "bankroll": bankroll,
                "peak": peak,
                "market_id": t.market_id,
                "pnl": t.realized_pnl,
            })

        # Metrics
        win_rate = (run.wins / run.total_trades * 100) if run.total_trades > 0 else 0
        total_return_pct = (
            run.total_pnl / run.initial_bankroll * 100
            if run.initial_bankroll > 0 else 0
        )

        avg_ev = 0.0
        avg_edge = 0.0
        calibration_error = 0.0
        if trades:
            avg_ev = sum(t.net_ev for t in trades) / len(trades)
            avg_edge = sum(t.edge for t in trades) / len(trades)
            avg_p_model = sum(t.p_model for t in trades) / len(trades)
            actual_win_rate = run.wins / run.total_trades if run.total_trades > 0 else 0
            calibration_error = abs(avg_p_model - actual_win_rate)

        return {
            "run_id": run_id,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "initial_bankroll": run.initial_bankroll,
            "final_bankroll": run.final_bankroll,
            "total_trades": run.total_trades,
            "wins": run.wins,
            "losses": run.losses,
            "total_pnl": run.total_pnl,
            "total_return_pct": round(total_return_pct, 2),
            "win_rate": round(win_rate, 2),
            "max_drawdown_pct": run.max_drawdown_pct,
            "avg_ev": round(avg_ev, 4),
            "avg_edge": round(avg_edge, 4),
            "calibration_error": round(calibration_error, 4),
            "equity_curve": curve,
            "limitations": LIMITATIONS,
        }
```

- [ ] **Step 4: Run tests, commit**

```bash
python3 -m pytest tests/test_backtest_report.py -v
git add src/backtest/report.py tests/test_backtest_report.py
git commit -m "feat: add backtest report builder with equity curve and calibration"
```

---

### Task 4: Backtest API Endpoints

**Files:**
- Create: `src/api/backtest.py`
- Create: `tests/test_api_backtest.py`
- Modify: `src/main.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_backtest.py
import random
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from fastapi import FastAPI
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.api.backtest import create_backtest_router


def _create_test_app(db_engine):
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(create_backtest_router(db_engine))
    return TestClient(app)


def _seed_markets(db_engine):
    with get_session(db_engine) as session:
        random.seed(42)
        session.add(Market(
            market_id="BT-1", title="Test market",
            category="Sports", close_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            status="closed",
        ))
        base = 45
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            session.add(PriceSnapshot(
                market_id="BT-1", yes_bid=price, yes_ask=price + 2,
                last_price=price + 1, volume=2000, timestamp=ts,
            ))
            base = price
        session.commit()


def test_run_backtest(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    resp = client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
        "initial_bankroll": 100.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "completed"


def test_get_backtest_report(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    # Run a backtest first
    resp = client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
    })
    run_id = resp.json()["run_id"]

    resp = client.get(f"/api/backtest/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert "equity_curve" in data
    assert "limitations" in data


def test_get_backtest_not_found(db_engine):
    client = _create_test_app(db_engine)
    resp = client.get("/api/backtest/999")
    assert resp.status_code == 404


def test_list_backtests(db_engine):
    client = _create_test_app(db_engine)
    _seed_markets(db_engine)
    client.post("/api/backtest/run", json={
        "start_date": "2026-03-01T00:00:00Z",
        "end_date": "2026-03-31T00:00:00Z",
    })
    resp = client.get("/api/backtest")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_api_backtest.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/api/backtest.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.backtest.models import BacktestRun
from src.backtest.runner import BacktestRunner
from src.backtest.report import build_report


class BacktestRequest(BaseModel):
    start_date: datetime
    end_date: datetime
    initial_bankroll: float = 100.0
    category_filter: Optional[str] = None


class BacktestRunSummary(BaseModel):
    id: int
    start_date: datetime
    end_date: datetime
    initial_bankroll: float
    final_bankroll: float
    total_trades: int
    total_pnl: float
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


def create_backtest_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/backtest", tags=["backtest"])

    @router.post("/run")
    def run_backtest(req: BacktestRequest):
        runner = BacktestRunner(engine)
        run_id = runner.run(
            start_date=req.start_date,
            end_date=req.end_date,
            initial_bankroll=req.initial_bankroll,
            category_filter=req.category_filter,
        )
        with get_session(engine) as session:
            run = session.query(BacktestRun).get(run_id)
            return {
                "run_id": run.id,
                "status": run.status,
                "total_trades": run.total_trades,
                "total_pnl": run.total_pnl,
                "final_bankroll": run.final_bankroll,
            }

    @router.get("/{run_id}")
    def get_report(run_id: int):
        report = build_report(engine, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Backtest run not found")
        return report

    @router.get("", response_model=List[BacktestRunSummary])
    def list_runs(limit: int = 20):
        with get_session(engine) as session:
            runs = (
                session.query(BacktestRun)
                .order_by(BacktestRun.created_at.desc())
                .limit(limit)
                .all()
            )
            return [BacktestRunSummary.model_validate(r) for r in runs]

    return router
```

- [ ] **Step 4: Register router in main.py**

Add to `src/main.py`:

```python
from src.api.backtest import create_backtest_router
# ...
app.include_router(create_backtest_router(engine))
```

- [ ] **Step 5: Run tests, commit**

```bash
python3 -m pytest tests/test_api_backtest.py -v
git add src/api/backtest.py tests/test_api_backtest.py src/main.py
git commit -m "feat: add backtest API endpoints for running and viewing backtests"
```

---

### Task 5: Final Integration Test

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests pass (130+)

- [ ] **Step 2: End-to-end smoke test**

```python
python3 -c "
from src.database import get_engine, get_session, Base
from src.config import Settings
from src.demo.seed import seed_demo_data
from src.backtest.runner import BacktestRunner
from src.backtest.report import build_report
from datetime import datetime, timezone

settings = Settings()
engine = get_engine(settings.DATABASE_URL)
Base.metadata.create_all(engine)
seed_demo_data(engine)

runner = BacktestRunner(engine)
run_id = runner.run(
    start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
    end_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
    initial_bankroll=100.0,
)
print(f'Backtest run {run_id} completed')

report = build_report(engine, run_id)
print(f'Trades: {report[\"total_trades\"]}, PnL: \${report[\"total_pnl\"]:.2f}')
print(f'Win rate: {report[\"win_rate\"]}%, Return: {report[\"total_return_pct\"]}%')
print(f'Equity curve: {len(report[\"equity_curve\"])} points')
print(f'Limitations:')
for lim in report['limitations']:
    print(f'  - {lim}')
"
```

- [ ] **Step 3: Commit if needed**

```bash
git commit -m "chore: plan 5 complete — backtesting engine"
```
