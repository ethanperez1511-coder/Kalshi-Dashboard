# Plan 4: Portfolio Tracking

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the portfolio tracking layer — position closing/settlement, bankroll updates, performance metrics (return, win rate, drawdown, calibration), trade log queries, and API endpoints to expose all of it to the dashboard.

**Architecture:** A `PortfolioTracker` service reads positions and trades from the DB, computes aggregate metrics, and handles position closing (updating bankroll + peak). API endpoints expose portfolio summary, trade log, and equity history. All computation is derived from existing `Position`, `Trade`, and `TradingSettings` models — no new DB tables needed.

**Tech Stack:** Python 3.9, SQLAlchemy, pydantic, FastAPI

---

## File Structure

```
src/
├── portfolio/
│   ├── __init__.py
│   ├── tracker.py           # PortfolioTracker: close positions, update bankroll
│   ├── metrics.py           # Compute performance metrics from trade history
│   └── equity.py            # Equity curve builder (bankroll over time)
├── api/
│   └── portfolio.py         # FastAPI endpoints for portfolio data
tests/
├── test_portfolio_tracker.py
├── test_portfolio_metrics.py
├── test_portfolio_equity.py
├── test_api_portfolio.py
```

---

### Task 1: Portfolio Tracker — Close Positions

**Files:**
- Create: `src/portfolio/__init__.py`
- Create: `src/portfolio/tracker.py`
- Create: `tests/test_portfolio_tracker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_portfolio_tracker.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.tracker import PortfolioTracker


def _setup(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Position(
            market_id="MKT-1", side="yes", entry_price=60,
            quantity=3, current_price=60, status="open",
        ))
        session.add(Trade(
            market_id="MKT-1", side="yes", action="buy", price=60,
            quantity=3, p_model=0.75, implied_prob=0.60, edge=0.15,
            net_ev=0.14, position_size_dollars=1.80, confidence=0.85,
            reasoning="test", is_paper=True, status="filled",
        ))
        session.commit()


def test_close_position_win(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="MKT-1", exit_price=100)
    assert result["realized_pnl"] > 0
    assert result["status"] == "closed"

    # Position should be closed
    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="MKT-1").first()
        assert pos.status == "closed"
        assert pos.closed_at is not None

    # Trade should be updated
    with get_session(db_engine) as session:
        trade = session.query(Trade).filter_by(market_id="MKT-1").first()
        assert trade.status == "closed"
        assert trade.exit_price == 100
        assert trade.realized_pnl > 0


def test_close_position_loss(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="MKT-1", exit_price=0)
    assert result["realized_pnl"] < 0

    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="MKT-1").first()
        assert pos.status == "closed"


def test_close_updates_bankroll(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    # Win: exit at 100, entry at 60, qty 3 → PnL = (100-60)*3/100 = 1.20
    tracker.close_position(market_id="MKT-1", exit_price=100)
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.bankroll == 101.20  # 100 + 1.20


def test_close_updates_peak_bankroll(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    tracker.close_position(market_id="MKT-1", exit_price=100)
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.peak_bankroll == 101.20


def test_close_nonexistent_returns_none(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    tracker = PortfolioTracker(db_engine)
    result = tracker.close_position(market_id="NOPE", exit_price=100)
    assert result is None


def test_get_open_positions(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    positions = tracker.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["market_id"] == "MKT-1"
    assert positions[0]["unrealized_pnl"] == 0.0  # current == entry


def test_get_portfolio_summary(db_engine):
    _setup(db_engine)
    tracker = PortfolioTracker(db_engine)
    summary = tracker.get_summary()
    assert summary["bankroll"] == 100.0
    assert summary["open_position_count"] == 1
    assert summary["total_exposure"] > 0
    assert "total_return_pct" in summary
    assert "max_drawdown_pct" in summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_portfolio_tracker.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/portfolio/__init__.py
```

```python
# src/portfolio/tracker.py
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from sqlalchemy import Engine
from src.database import get_session
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)


class PortfolioTracker:
    def __init__(self, engine: Engine):
        self._engine = engine

    def close_position(
        self, market_id: str, exit_price: int,
    ) -> Optional[Dict[str, Any]]:
        with get_session(self._engine) as session:
            pos = (
                session.query(Position)
                .filter_by(market_id=market_id, status="open")
                .first()
            )
            if not pos:
                return None

            # Calculate realized PnL
            if pos.side == "yes":
                realized_pnl = (exit_price - pos.entry_price) * pos.quantity / 100.0
            else:
                realized_pnl = (pos.entry_price - exit_price) * pos.quantity / 100.0

            # Close the position
            pos.status = "closed"
            pos.current_price = exit_price
            pos.closed_at = datetime.now(timezone.utc)

            # Update the trade record
            trade = (
                session.query(Trade)
                .filter_by(market_id=market_id, status="filled")
                .order_by(Trade.created_at.desc())
                .first()
            )
            if trade:
                trade.status = "closed"
                trade.exit_price = exit_price
                trade.realized_pnl = realized_pnl

            # Update bankroll
            settings = session.query(TradingSettings).first()
            if settings:
                settings.bankroll = round(settings.bankroll + realized_pnl, 2)
                if settings.bankroll > settings.peak_bankroll:
                    settings.peak_bankroll = settings.bankroll

            session.commit()

            logger.info(
                f"Closed {market_id} @ {exit_price}c — PnL ${realized_pnl:.2f}"
            )

            return {
                "market_id": market_id,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "status": "closed",
            }

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with get_session(self._engine) as session:
            positions = session.query(Position).filter_by(status="open").all()
            return [
                {
                    "market_id": p.market_id,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "quantity": p.quantity,
                    "unrealized_pnl": p.unrealized_pnl,
                    "cost_basis": p.cost_basis,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                }
                for p in positions
            ]

    def get_summary(self) -> Dict[str, Any]:
        with get_session(self._engine) as session:
            settings = session.query(TradingSettings).first()
            if not settings:
                return {
                    "bankroll": 0, "open_position_count": 0,
                    "total_exposure": 0, "total_return_pct": 0,
                    "max_drawdown_pct": 0, "unrealized_pnl": 0,
                }

            positions = session.query(Position).filter_by(status="open").all()
            total_exposure = sum(p.cost_basis for p in positions)
            unrealized_pnl = sum(p.unrealized_pnl for p in positions)

            initial_bankroll = 100.0  # Starting capital
            total_return_pct = (
                (settings.bankroll - initial_bankroll) / initial_bankroll * 100
                if initial_bankroll > 0 else 0
            )

            max_drawdown_pct = 0.0
            if settings.peak_bankroll > 0:
                max_drawdown_pct = (
                    (settings.peak_bankroll - settings.bankroll)
                    / settings.peak_bankroll * 100
                )

            return {
                "bankroll": settings.bankroll,
                "peak_bankroll": settings.peak_bankroll,
                "open_position_count": len(positions),
                "total_exposure": round(total_exposure, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_return_pct": round(total_return_pct, 2),
                "max_drawdown_pct": round(max_drawdown_pct, 2),
            }
```

- [ ] **Step 4: Run tests, commit**

```bash
python3 -m pytest tests/test_portfolio_tracker.py -v
git add src/portfolio/__init__.py src/portfolio/tracker.py tests/test_portfolio_tracker.py
git commit -m "feat: add portfolio tracker with position closing and bankroll updates"
```

---

### Task 2: Performance Metrics

**Files:**
- Create: `src/portfolio/metrics.py`
- Create: `tests/test_portfolio_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_portfolio_metrics.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.metrics import compute_metrics


def _seed_closed_trades(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        trades = [
            Trade(
                market_id="W1", side="yes", action="buy", price=50, quantity=2,
                p_model=0.70, implied_prob=0.50, edge=0.20, net_ev=0.19,
                position_size_dollars=1.0, confidence=0.85, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.0,
                created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="W2", side="yes", action="buy", price=60, quantity=2,
                p_model=0.80, implied_prob=0.60, edge=0.20, net_ev=0.19,
                position_size_dollars=1.20, confidence=0.80, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=0.80,
                created_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="L1", side="yes", action="buy", price=55, quantity=2,
                p_model=0.65, implied_prob=0.55, edge=0.10, net_ev=0.09,
                position_size_dollars=1.10, confidence=0.75, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-1.10,
                created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()


def test_metrics_win_rate(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    # 2 wins, 1 loss
    assert abs(m["win_rate"] - 66.67) < 0.1


def test_metrics_total_pnl(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    # 1.0 + 0.80 - 1.10 = 0.70
    assert abs(m["total_pnl"] - 0.70) < 0.01


def test_metrics_total_return_pct(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    # 0.70 / 100 * 100 = 0.7%
    assert abs(m["total_return_pct"] - 0.70) < 0.01


def test_metrics_average_edge(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    # (0.20 + 0.20 + 0.10) / 3 ≈ 0.1667
    assert abs(m["avg_edge"] - 0.1667) < 0.01


def test_metrics_trade_count(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    assert m["total_trades"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1


def test_metrics_calibration(db_engine):
    _seed_closed_trades(db_engine)
    m = compute_metrics(db_engine)
    # avg p_model = (0.70+0.80+0.65)/3 ≈ 0.717
    # actual win rate = 2/3 ≈ 0.667
    # calibration_error = |0.717 - 0.667| ≈ 0.05
    assert "calibration_error" in m
    assert m["calibration_error"] >= 0


def test_metrics_empty(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    m = compute_metrics(db_engine)
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0
    assert m["total_pnl"] == 0
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_portfolio_metrics.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/portfolio/metrics.py
from __future__ import annotations
from typing import Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.settings import TradingSettings


def compute_metrics(engine: Engine) -> Dict[str, Any]:
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        bankroll = settings.bankroll if settings else 100.0
        initial_bankroll = 100.0

        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.created_at)
            .all()
        )

        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "total_return_pct": 0,
                "avg_edge": 0, "avg_ev": 0, "calibration_error": 0,
                "avg_pnl_per_trade": 0,
            }

        wins = [t for t in trades if (t.realized_pnl or 0) > 0]
        losses = [t for t in trades if (t.realized_pnl or 0) <= 0]
        total_pnl = sum(t.realized_pnl or 0 for t in trades)
        avg_edge = sum(t.edge for t in trades) / len(trades)
        avg_ev = sum(t.net_ev for t in trades) / len(trades)

        win_rate = len(wins) / len(trades) * 100
        total_return_pct = total_pnl / initial_bankroll * 100

        # Calibration: avg predicted probability vs actual win frequency
        avg_p_model = sum(t.p_model for t in trades) / len(trades)
        actual_win_rate = len(wins) / len(trades)
        calibration_error = abs(avg_p_model - actual_win_rate)

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "avg_edge": round(avg_edge, 4),
            "avg_ev": round(avg_ev, 4),
            "calibration_error": round(calibration_error, 4),
            "avg_pnl_per_trade": round(total_pnl / len(trades), 4),
        }
```

- [ ] **Step 4: Run tests, commit**

```bash
python3 -m pytest tests/test_portfolio_metrics.py -v
git add src/portfolio/metrics.py tests/test_portfolio_metrics.py
git commit -m "feat: add portfolio performance metrics computation"
```

---

### Task 3: Equity Curve Builder

**Files:**
- Create: `src/portfolio/equity.py`
- Create: `tests/test_portfolio_equity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_portfolio_equity.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.portfolio.equity import build_equity_curve


def _seed_trades(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        trades = [
            Trade(
                market_id="T1", side="yes", action="buy", price=50, quantity=2,
                p_model=0.7, implied_prob=0.5, edge=0.2, net_ev=0.19,
                position_size_dollars=1.0, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.0,
                created_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="T2", side="yes", action="buy", price=60, quantity=2,
                p_model=0.8, implied_prob=0.6, edge=0.2, net_ev=0.19,
                position_size_dollars=1.2, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-1.20,
                created_at=datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc),
            ),
            Trade(
                market_id="T3", side="yes", action="buy", price=40, quantity=2,
                p_model=0.6, implied_prob=0.4, edge=0.2, net_ev=0.19,
                position_size_dollars=0.8, confidence=0.7, reasoning="test",
                is_paper=True, status="closed", exit_price=100, realized_pnl=1.20,
                created_at=datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()


def test_equity_curve_length(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    # 3 trades + 1 starting point = 4 data points
    assert len(curve) == 4


def test_equity_curve_starts_at_initial_bankroll(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    assert curve[0]["bankroll"] == 100.0


def test_equity_curve_cumulative(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    # After trade 1: 100 + 1.0 = 101.0
    assert abs(curve[1]["bankroll"] - 101.0) < 0.01
    # After trade 2: 101.0 - 1.20 = 99.80
    assert abs(curve[2]["bankroll"] - 99.80) < 0.01
    # After trade 3: 99.80 + 1.20 = 101.0
    assert abs(curve[3]["bankroll"] - 101.0) < 0.01


def test_equity_curve_tracks_peak(db_engine):
    _seed_trades(db_engine)
    curve = build_equity_curve(db_engine)
    # Peak after trade 1 is 101.0, should persist through dip
    assert curve[1]["peak"] == 101.0
    assert curve[2]["peak"] == 101.0
    assert curve[3]["peak"] == 101.0


def test_equity_curve_empty(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    curve = build_equity_curve(db_engine)
    assert len(curve) == 1
    assert curve[0]["bankroll"] == 100.0
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_portfolio_equity.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/portfolio/equity.py
from __future__ import annotations
from typing import Dict, Any, List
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.settings import TradingSettings


def build_equity_curve(
    engine: Engine, initial_bankroll: float = 100.0,
) -> List[Dict[str, Any]]:
    with get_session(engine) as session:
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.created_at)
            .all()
        )

        curve = [{"timestamp": None, "bankroll": initial_bankroll, "peak": initial_bankroll}]

        bankroll = initial_bankroll
        peak = initial_bankroll

        for trade in trades:
            bankroll = round(bankroll + (trade.realized_pnl or 0), 2)
            peak = max(peak, bankroll)
            curve.append({
                "timestamp": trade.created_at.isoformat() if trade.created_at else None,
                "bankroll": bankroll,
                "peak": peak,
                "market_id": trade.market_id,
                "pnl": trade.realized_pnl or 0,
            })

        return curve
```

- [ ] **Step 4: Run tests, commit**

```bash
python3 -m pytest tests/test_portfolio_equity.py -v
git add src/portfolio/equity.py tests/test_portfolio_equity.py
git commit -m "feat: add equity curve builder for portfolio history"
```

---

### Task 4: Portfolio API Endpoints

**Files:**
- Create: `src/api/portfolio.py`
- Create: `tests/test_api_portfolio.py`
- Modify: `src/main.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_portfolio.py
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.database import get_engine, get_session, Base
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings
from src.api.portfolio import create_portfolio_router
from fastapi import FastAPI


def _create_test_app(db_engine):
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(create_portfolio_router(db_engine))
    return TestClient(app)


def _seed_data(db_engine):
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Position(
            market_id="MKT-1", side="yes", entry_price=60,
            quantity=3, current_price=65, status="open",
        ))
        session.add(Trade(
            market_id="MKT-1", side="yes", action="buy", price=60,
            quantity=3, p_model=0.75, implied_prob=0.60, edge=0.15,
            net_ev=0.14, position_size_dollars=1.80, confidence=0.85,
            reasoning="test trade", is_paper=True, status="filled",
            created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        ))
        session.add(Trade(
            market_id="MKT-0", side="yes", action="buy", price=50,
            quantity=2, p_model=0.70, implied_prob=0.50, edge=0.20,
            net_ev=0.19, position_size_dollars=1.0, confidence=0.80,
            reasoning="closed trade", is_paper=True, status="closed",
            exit_price=100, realized_pnl=1.0,
            created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        ))
        session.commit()


def test_get_summary(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "bankroll" in data
    assert "open_position_count" in data
    assert data["open_position_count"] == 1


def test_get_positions(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["market_id"] == "MKT-1"


def test_get_trades(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/trades")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_get_trades_filter_status(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/trades?status=closed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["market_id"] == "MKT-0"


def test_get_metrics(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 1
    assert data["wins"] == 1


def test_get_equity_curve(db_engine):
    client = _create_test_app(db_engine)
    _seed_data(db_engine)
    resp = client.get("/api/portfolio/equity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["bankroll"] == 100.0
```

- [ ] **Step 2: Run test, verify fail**

Run: `python3 -m pytest tests/test_api_portfolio.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/api/portfolio.py
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import Engine

from src.database import get_session
from src.models.trade import Trade
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.metrics import compute_metrics
from src.portfolio.equity import build_equity_curve


class PositionResponse(BaseModel):
    market_id: str
    side: str
    entry_price: int
    current_price: int
    quantity: int
    unrealized_pnl: float
    cost_basis: float
    opened_at: Optional[str]


class TradeResponse(BaseModel):
    market_id: str
    side: str
    action: str
    price: int
    quantity: int
    p_model: float
    implied_prob: float
    edge: float
    net_ev: float
    position_size_dollars: float
    confidence: float
    reasoning: str
    is_paper: bool
    status: str
    exit_price: Optional[int]
    realized_pnl: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True


def create_portfolio_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
    tracker = PortfolioTracker(engine)

    @router.get("/summary")
    def get_summary():
        return tracker.get_summary()

    @router.get("/positions", response_model=List[PositionResponse])
    def get_positions():
        return tracker.get_open_positions()

    @router.get("/trades", response_model=List[TradeResponse])
    def get_trades(status: Optional[str] = None, limit: int = 100):
        with get_session(engine) as session:
            query = session.query(Trade)
            if status:
                query = query.filter(Trade.status == status)
            trades = query.order_by(Trade.created_at.desc()).limit(limit).all()
            return [TradeResponse.model_validate(t) for t in trades]

    @router.get("/metrics")
    def get_metrics():
        return compute_metrics(engine)

    @router.get("/equity")
    def get_equity():
        return build_equity_curve(engine)

    return router
```

- [ ] **Step 4: Register router in main.py**

Add to `src/main.py` after the markets router:

```python
from src.api.portfolio import create_portfolio_router
# ...
app.include_router(create_portfolio_router(engine))
```

- [ ] **Step 5: Run tests, commit**

```bash
python3 -m pytest tests/test_api_portfolio.py -v
git add src/api/portfolio.py tests/test_api_portfolio.py src/main.py
git commit -m "feat: add portfolio API endpoints for summary, trades, metrics, equity"
```

---

### Task 5: Final Integration Test

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests pass (100+)

- [ ] **Step 2: End-to-end smoke test**

```python
python3 -c "
from src.database import get_engine, get_session, Base
from src.models.settings import TradingSettings
from src.models.opportunity import Opportunity
from src.config import Settings
from src.demo.seed import seed_demo_data
from src.ev.scorer import score_all_markets
from src.risk.manager import RiskManager
from src.trading.engine import TradeEngine
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.metrics import compute_metrics
from src.portfolio.equity import build_equity_curve

settings = Settings()
engine = get_engine(settings.DATABASE_URL)
Base.metadata.create_all(engine)

# Setup
seed_demo_data(engine)
TradingSettings.get_or_create(engine)

# Score markets
results = score_all_markets(engine)
qualifying = [r for r in results if r['status'] == 'qualifying']
print(f'Scored {len(results)} markets, {len(qualifying)} qualifying')

# Execute trades on qualifying markets
rm = RiskManager(engine)
te = TradeEngine(engine)
from src.ev.calculator import EVResult
for opp in qualifying:
    ev = EVResult(
        p_model=opp['p_model'], implied_prob=opp['implied_prob'],
        edge=opp['edge'], no_edge=-opp['edge'],
        raw_ev=opp['net_ev'], net_ev=opp['net_ev'], no_ev=-opp['net_ev'],
        recommended_side=opp['recommended_side'], fee_rate=0.01,
    )
    decision = rm.evaluate(ev, opp['confidence'], opp['market_id'], 'Sports')
    result = te.execute(
        decision, opp['market_id'], opp['p_model'], opp['implied_prob'],
        opp['edge'], opp['net_ev'], opp['confidence'], opp.get('reasoning', 'auto'),
    )
    if result:
        print(f'  Traded: {result[\"market_id\"]} {result[\"side\"]} x{result[\"quantity\"]}')

# Portfolio summary
tracker = PortfolioTracker(engine)
summary = tracker.get_summary()
print(f'Portfolio: bankroll=\${summary[\"bankroll\"]:.2f}, positions={summary[\"open_position_count\"]}, exposure=\${summary[\"total_exposure\"]:.2f}')

# Close a position to test metrics
positions = tracker.get_open_positions()
if positions:
    tracker.close_position(positions[0]['market_id'], exit_price=100)
    metrics = compute_metrics(engine)
    print(f'Metrics: trades={metrics[\"total_trades\"]}, win_rate={metrics[\"win_rate\"]}%, pnl=\${metrics[\"total_pnl\"]:.2f}')
    curve = build_equity_curve(engine)
    print(f'Equity curve: {len(curve)} points, final=\${curve[-1][\"bankroll\"]:.2f}')
"
```

- [ ] **Step 3: Commit if needed**

```bash
git commit -m "chore: plan 4 complete — portfolio tracking"
```
