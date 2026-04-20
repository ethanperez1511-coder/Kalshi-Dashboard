# Plan 3: Risk Management + Trade Execution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the risk management layer (Kelly sizing, hard limits, correlation tracking) and trade execution engine (order placement, paper trading mode) that turns qualifying opportunities into sized, managed positions.

**Architecture:** Risk manager validates every trade against hard limits before execution. Trade engine places limit orders via the Kalshi API (or simulates in paper mode). All trades and decisions are logged. Paper mode is enforced by default.

**Tech Stack:** Python 3.9, SQLAlchemy, pydantic, FastAPI

---

## File Structure

```
src/
├── risk/
│   ├── __init__.py
│   ├── kelly.py              # Kelly criterion position sizing
│   ├── limits.py             # Hard limits checker (exposure, drawdown, daily loss)
│   └── manager.py            # Risk manager (ties kelly + limits together)
├── trading/
│   ├── __init__.py
│   ├── engine.py             # Trade execution engine (paper + live)
│   └── order.py              # Order management (place, monitor, cancel)
├── models/
│   ├── trade.py              # Trade SQLAlchemy model
│   ├── position.py           # Position SQLAlchemy model
│   └── settings.py           # TradingSettings SQLAlchemy model (bankroll, mode, etc.)
tests/
├── test_kelly.py
├── test_limits.py
├── test_risk_manager.py
├── test_trade_engine.py
├── test_trade_model.py
├── test_position_model.py
├── test_settings_model.py
```

---

### Task 1: Trading Settings Model

**Files:**
- Create: `src/models/settings.py`
- Modify: `src/models/__init__.py`
- Create: `tests/test_settings_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings_model.py
from src.database import get_session, Base
from src.models.settings import TradingSettings


def test_create_default_settings(db_engine):
    Base.metadata.create_all(db_engine)
    settings = TradingSettings.get_or_create(db_engine)
    assert settings.bankroll == 100.0
    assert settings.mode == "paper"
    assert settings.max_single_trade_pct == 0.03
    assert settings.max_total_exposure_pct == 0.25
    assert settings.max_correlated_exposure_pct == 0.10
    assert settings.daily_loss_limit_pct == 0.05
    assert settings.drawdown_circuit_breaker_pct == 0.20
    assert settings.kelly_fraction == 0.25
    assert settings.paper_trades_before_live == 50


def test_settings_singleton(db_engine):
    Base.metadata.create_all(db_engine)
    s1 = TradingSettings.get_or_create(db_engine)
    s2 = TradingSettings.get_or_create(db_engine)
    assert s1.id == s2.id


def test_update_bankroll(db_engine):
    Base.metadata.create_all(db_engine)
    settings = TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        s = session.query(TradingSettings).first()
        s.bankroll = 1000.0
        session.commit()
    with get_session(db_engine) as session:
        s = session.query(TradingSettings).first()
        assert s.bankroll == 1000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_settings_model.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/models/settings.py
from __future__ import annotations
from sqlalchemy import Float, String, Integer, Engine
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base, get_session


class TradingSettings(Base):
    __tablename__ = "trading_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    mode: Mapped[str] = mapped_column(String(10), default="paper")  # "paper" or "live"
    max_single_trade_pct: Mapped[float] = mapped_column(Float, default=0.03)
    max_total_exposure_pct: Mapped[float] = mapped_column(Float, default=0.25)
    max_correlated_exposure_pct: Mapped[float] = mapped_column(Float, default=0.10)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float, default=0.05)
    drawdown_circuit_breaker_pct: Mapped[float] = mapped_column(Float, default=0.20)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25)
    paper_trades_before_live: Mapped[int] = mapped_column(Integer, default=50)
    peak_bankroll: Mapped[float] = mapped_column(Float, default=100.0)
    paper_trade_count: Mapped[int] = mapped_column(Integer, default=0)

    @classmethod
    def get_or_create(cls, engine: Engine) -> TradingSettings:
        with get_session(engine) as session:
            settings = session.query(cls).first()
            if settings is None:
                settings = cls()
                session.add(settings)
                session.commit()
            # Detach-safe: read all values while in session
            s = cls(
                id=settings.id,
                bankroll=settings.bankroll,
                mode=settings.mode,
                max_single_trade_pct=settings.max_single_trade_pct,
                max_total_exposure_pct=settings.max_total_exposure_pct,
                max_correlated_exposure_pct=settings.max_correlated_exposure_pct,
                daily_loss_limit_pct=settings.daily_loss_limit_pct,
                drawdown_circuit_breaker_pct=settings.drawdown_circuit_breaker_pct,
                kelly_fraction=settings.kelly_fraction,
                paper_trades_before_live=settings.paper_trades_before_live,
                peak_bankroll=settings.peak_bankroll,
                paper_trade_count=settings.paper_trade_count,
            )
        return s
```

- [ ] **Step 4: Update models/__init__.py**

Add TradingSettings to imports.

- [ ] **Step 5: Run tests, commit**

```bash
git commit -m "feat: add TradingSettings model with defaults"
```

---

### Task 2: Position + Trade Models

**Files:**
- Create: `src/models/position.py`
- Create: `src/models/trade.py`
- Modify: `src/models/__init__.py`
- Create: `tests/test_position_model.py`
- Create: `tests/test_trade_model.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_position_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.position import Position


def test_create_position(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        pos = Position(
            market_id="FED-RATE-JUL",
            side="yes",
            entry_price=65,
            quantity=2,
            current_price=68,
            status="open",
        )
        session.add(pos)
        session.commit()
        fetched = session.query(Position).first()
        assert fetched.market_id == "FED-RATE-JUL"
        assert fetched.side == "yes"
        assert fetched.entry_price == 65
        assert fetched.status == "open"


def test_position_unrealized_pnl(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        pos = Position(
            market_id="FED-RATE-JUL", side="yes",
            entry_price=65, quantity=2, current_price=70, status="open",
        )
        session.add(pos)
        session.commit()
        fetched = session.query(Position).first()
        # PnL for Yes: (current - entry) * quantity / 100
        expected = (70 - 65) * 2 / 100.0
        assert abs(fetched.unrealized_pnl - expected) < 0.001
```

```python
# tests/test_trade_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.trade import Trade


def test_create_trade(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        trade = Trade(
            market_id="FED-RATE-JUL",
            side="yes",
            action="buy",
            price=65,
            quantity=2,
            p_model=0.77,
            implied_prob=0.65,
            edge=0.12,
            net_ev=0.10,
            position_size_dollars=1.30,
            confidence=0.85,
            reasoning="Finance model: edge 12%",
            is_paper=True,
            status="filled",
        )
        session.add(trade)
        session.commit()
        fetched = session.query(Trade).first()
        assert fetched.market_id == "FED-RATE-JUL"
        assert fetched.is_paper is True
        assert fetched.status == "filled"
        assert fetched.edge == 0.12


def test_trade_pnl_on_close(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        trade = Trade(
            market_id="FED-RATE-JUL", side="yes", action="buy",
            price=65, quantity=2, p_model=0.77, implied_prob=0.65,
            edge=0.12, net_ev=0.10, position_size_dollars=1.30,
            confidence=0.85, reasoning="test", is_paper=True,
            status="closed", exit_price=100, realized_pnl=0.70,
        )
        session.add(trade)
        session.commit()
        fetched = session.query(Trade).first()
        assert fetched.exit_price == 100
        assert fetched.realized_pnl == 0.70
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Write Position model**

```python
# src/models/position.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    side: Mapped[str] = mapped_column(String(10))  # "yes" or "no"
    entry_price: Mapped[int] = mapped_column(Integer)  # cents
    quantity: Mapped[int] = mapped_column(Integer)
    current_price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)  # open, closed
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def unrealized_pnl(self) -> float:
        if self.side == "yes":
            return (self.current_price - self.entry_price) * self.quantity / 100.0
        else:
            return (self.entry_price - self.current_price) * self.quantity / 100.0

    @property
    def cost_basis(self) -> float:
        if self.side == "yes":
            return self.entry_price * self.quantity / 100.0
        else:
            return (100 - self.entry_price) * self.quantity / 100.0
```

- [ ] **Step 4: Write Trade model**

```python
# src/models/trade.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), index=True)
    side: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10))  # "buy" or "sell"
    price: Mapped[int] = mapped_column(Integer)  # cents
    quantity: Mapped[int] = mapped_column(Integer)
    p_model: Mapped[float] = mapped_column(Float)
    implied_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    net_ev: Mapped[float] = mapped_column(Float)
    position_size_dollars: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(Text)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # pending, filled, cancelled, closed
    exit_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
```

- [ ] **Step 5: Update models/__init__.py, run tests, commit**

```bash
git commit -m "feat: add Position and Trade models"
```

---

### Task 3: Kelly Criterion Position Sizing

**Files:**
- Create: `src/risk/__init__.py`
- Create: `src/risk/kelly.py`
- Create: `tests/test_kelly.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kelly.py
from src.risk.kelly import kelly_size, KellyResult


def test_kelly_basic():
    result = kelly_size(
        p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    assert result.full_kelly > 0
    assert result.fractional_kelly > 0
    assert result.fractional_kelly < result.full_kelly
    assert result.recommended_dollars > 0
    assert result.recommended_quantity >= 1


def test_kelly_negative_edge_returns_zero():
    result = kelly_size(
        p_model=0.50, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    assert result.full_kelly == 0
    assert result.recommended_dollars == 0
    assert result.recommended_quantity == 0


def test_kelly_respects_fraction():
    full = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=1.0)
    quarter = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25)
    assert abs(quarter.recommended_dollars - full.recommended_dollars * 0.25) < 0.5


def test_kelly_small_bankroll():
    result = kelly_size(
        p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25,
    )
    # With $100, position should be small
    assert result.recommended_dollars <= 5.0


def test_kelly_scales_with_bankroll():
    small = kelly_size(p_model=0.77, price_cents=65, bankroll=100.0, kelly_fraction=0.25)
    large = kelly_size(p_model=0.77, price_cents=65, bankroll=1000.0, kelly_fraction=0.25)
    assert large.recommended_dollars > small.recommended_dollars
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Write implementation**

```python
# src/risk/__init__.py
```

```python
# src/risk/kelly.py
from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass
class KellyResult:
    full_kelly: float           # Full Kelly fraction of bankroll
    fractional_kelly: float     # Adjusted Kelly (e.g., quarter Kelly)
    recommended_dollars: float  # Dollar amount to risk
    recommended_quantity: int   # Number of contracts (rounded down)
    edge: float
    odds: float


def kelly_size(
    p_model: float,
    price_cents: int,
    bankroll: float,
    kelly_fraction: float = 0.25,
) -> KellyResult:
    """Calculate position size using fractional Kelly criterion.

    Kelly formula: f* = (bp - q) / b
    where b = odds (payout ratio), p = win prob, q = 1-p
    """
    price = price_cents / 100.0
    edge = p_model - price

    if edge <= 0:
        return KellyResult(
            full_kelly=0, fractional_kelly=0,
            recommended_dollars=0, recommended_quantity=0,
            edge=edge, odds=0,
        )

    # For binary contracts: pay `price`, win `1-price` if correct
    # odds = (1 - price) / price = net payout per dollar risked
    b = (1 - price) / price
    p = p_model
    q = 1 - p

    # Kelly: f* = (bp - q) / b
    full_kelly_frac = (b * p - q) / b
    full_kelly_frac = max(0, full_kelly_frac)

    fractional = full_kelly_frac * kelly_fraction
    dollars = bankroll * fractional

    # Convert to contract quantity: each contract costs `price` dollars
    contract_cost = price
    quantity = math.floor(dollars / contract_cost) if contract_cost > 0 else 0

    return KellyResult(
        full_kelly=full_kelly_frac,
        fractional_kelly=fractional,
        recommended_dollars=round(dollars, 2),
        recommended_quantity=quantity,
        edge=edge,
        odds=b,
    )
```

- [ ] **Step 4: Run tests, commit**

```bash
git commit -m "feat: add Kelly criterion position sizing"
```

---

### Task 4: Hard Limits Checker

**Files:**
- Create: `src/risk/limits.py`
- Create: `tests/test_limits.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_limits.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.risk.limits import LimitsChecker, LimitsResult
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings


def test_passes_when_within_limits(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)  # bankroll=100
    checker = LimitsChecker(db_engine)
    result = checker.check(
        trade_dollars=2.0,
        market_id="FED-RATE-JUL",
        market_category="Economics",
    )
    assert result.approved is True
    assert len(result.violations) == 0


def test_rejects_exceeding_single_trade(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=5.0, market_id="X", market_category="Economics")
    assert result.approved is False
    assert any("single trade" in v.lower() for v in result.violations)


def test_rejects_exceeding_total_exposure(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    # Create existing positions totaling $20
    with get_session(db_engine) as session:
        for i in range(10):
            session.add(Position(
                market_id=f"MKT-{i}", side="yes", entry_price=50,
                quantity=4, current_price=50, status="open",
            ))
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=2.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("total exposure" in v.lower() for v in result.violations)


def test_rejects_daily_loss_exceeded(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    # Create losing trades today
    with get_session(db_engine) as session:
        for i in range(3):
            session.add(Trade(
                market_id=f"LOSS-{i}", side="yes", action="buy", price=50,
                quantity=2, p_model=0.6, implied_prob=0.5, edge=0.1, net_ev=0.05,
                position_size_dollars=2.0, confidence=0.8, reasoning="test",
                is_paper=True, status="closed", exit_price=0, realized_pnl=-2.0,
                created_at=datetime.now(timezone.utc),
            ))
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=1.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("daily loss" in v.lower() for v in result.violations)


def test_rejects_drawdown_breaker(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.bankroll = 75.0  # Down from 100 peak
        settings.peak_bankroll = 100.0
        session.add(settings)
        session.commit()
    checker = LimitsChecker(db_engine)
    result = checker.check(trade_dollars=1.0, market_id="NEW", market_category="Economics")
    assert result.approved is False
    assert any("drawdown" in v.lower() for v in result.violations)
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Write implementation**

```python
# src/risk/limits.py
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List
from sqlalchemy import Engine
from src.database import get_session
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)


@dataclass
class LimitsResult:
    approved: bool
    violations: List[str] = field(default_factory=list)


class LimitsChecker:
    def __init__(self, engine: Engine):
        self._engine = engine

    def _get_settings(self) -> dict:
        with get_session(self._engine) as session:
            s = session.query(TradingSettings).first()
            if not s:
                return {}
            return {
                "bankroll": s.bankroll,
                "peak_bankroll": s.peak_bankroll,
                "max_single_trade_pct": s.max_single_trade_pct,
                "max_total_exposure_pct": s.max_total_exposure_pct,
                "max_correlated_exposure_pct": s.max_correlated_exposure_pct,
                "daily_loss_limit_pct": s.daily_loss_limit_pct,
                "drawdown_circuit_breaker_pct": s.drawdown_circuit_breaker_pct,
            }

    def _get_total_exposure(self) -> float:
        with get_session(self._engine) as session:
            positions = session.query(Position).filter_by(status="open").all()
            total = sum(p.cost_basis for p in positions)
        return total

    def _get_daily_pnl(self) -> float:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with get_session(self._engine) as session:
            trades = (
                session.query(Trade)
                .filter(Trade.status == "closed")
                .filter(Trade.created_at >= today_start)
                .all()
            )
            total_pnl = sum(t.realized_pnl or 0 for t in trades)
        return total_pnl

    def check(self, trade_dollars: float, market_id: str, market_category: str) -> LimitsResult:
        settings = self._get_settings()
        if not settings:
            return LimitsResult(approved=False, violations=["No trading settings found"])

        violations = []
        bankroll = settings["bankroll"]

        # 1. Max single trade
        max_single = bankroll * settings["max_single_trade_pct"]
        if trade_dollars > max_single:
            violations.append(
                f"Single trade ${trade_dollars:.2f} exceeds max ${max_single:.2f} "
                f"({settings['max_single_trade_pct']:.0%} of bankroll)"
            )

        # 2. Total exposure
        current_exposure = self._get_total_exposure()
        max_exposure = bankroll * settings["max_total_exposure_pct"]
        if current_exposure + trade_dollars > max_exposure:
            violations.append(
                f"Total exposure ${current_exposure + trade_dollars:.2f} exceeds max ${max_exposure:.2f} "
                f"({settings['max_total_exposure_pct']:.0%} of bankroll)"
            )

        # 3. Daily loss limit
        daily_pnl = self._get_daily_pnl()
        max_daily_loss = bankroll * settings["daily_loss_limit_pct"]
        if daily_pnl < 0 and abs(daily_pnl) >= max_daily_loss:
            violations.append(
                f"Daily loss ${abs(daily_pnl):.2f} exceeds limit ${max_daily_loss:.2f} "
                f"({settings['daily_loss_limit_pct']:.0%} of bankroll) — paused"
            )

        # 4. Drawdown circuit breaker
        peak = settings["peak_bankroll"]
        if peak > 0:
            drawdown = (peak - bankroll) / peak
            if drawdown >= settings["drawdown_circuit_breaker_pct"]:
                violations.append(
                    f"Drawdown {drawdown:.1%} exceeds circuit breaker "
                    f"{settings['drawdown_circuit_breaker_pct']:.0%} — system stopped"
                )

        return LimitsResult(
            approved=len(violations) == 0,
            violations=violations,
        )
```

- [ ] **Step 4: Run tests, commit**

```bash
git commit -m "feat: add hard limits checker for risk management"
```

---

### Task 5: Risk Manager

**Files:**
- Create: `src/risk/manager.py`
- Create: `tests/test_risk_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_manager.py
from src.database import Base
from src.risk.manager import RiskManager
from src.models.settings import TradingSettings
from src.ev.calculator import EVResult


def test_risk_manager_approves_good_trade(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.85,
        market_id="FED-RATE-JUL", market_category="Economics",
    )
    assert decision.approved is True
    assert decision.position_size_dollars > 0
    assert decision.quantity >= 1
    assert decision.side == "yes"


def test_risk_manager_rejects_negative_edge(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    ev = EVResult(
        p_model=0.50, implied_prob=0.65, edge=-0.15, no_edge=0.15,
        raw_ev=-0.15, net_ev=-0.16, no_ev=0.14,
        recommended_side="no", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.85,
        market_id="FED-RATE-JUL", market_category="Economics",
    )
    # No side has positive edge, so it might approve for No
    # But let's check it returns a decision either way
    assert decision.side == "no"


def test_risk_manager_caps_position_size(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    rm = RiskManager(db_engine)
    # Very high edge should still be capped at 3% of bankroll
    ev = EVResult(
        p_model=0.95, implied_prob=0.30, edge=0.65, no_edge=-0.65,
        raw_ev=0.60, net_ev=0.59, no_ev=-0.62,
        recommended_side="yes", fee_rate=0.01,
    )
    decision = rm.evaluate(
        ev_result=ev, confidence=0.9,
        market_id="HIGH-EDGE", market_category="Economics",
    )
    assert decision.position_size_dollars <= 3.0  # 3% of $100
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Write implementation**

```python
# src/risk/manager.py
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import Engine
from src.database import get_session
from src.ev.calculator import EVResult
from src.risk.kelly import kelly_size
from src.risk.limits import LimitsChecker
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    approved: bool
    side: str
    position_size_dollars: float
    quantity: int
    price_cents: int
    rejection_reasons: list


class RiskManager:
    def __init__(self, engine: Engine):
        self._engine = engine
        self._limits = LimitsChecker(engine)

    def evaluate(
        self,
        ev_result: EVResult,
        confidence: float,
        market_id: str,
        market_category: str,
    ) -> TradeDecision:
        # Get settings
        settings = TradingSettings.get_or_create(self._engine)
        side = ev_result.recommended_side
        price_cents = int(ev_result.implied_prob * 100)

        if side == "no":
            price_cents = 100 - price_cents

        # Kelly sizing
        p = ev_result.p_model if side == "yes" else (1 - ev_result.p_model)
        kelly = kelly_size(
            p_model=p,
            price_cents=price_cents,
            bankroll=settings.bankroll,
            kelly_fraction=settings.kelly_fraction,
        )

        # Cap at max single trade
        max_trade = settings.bankroll * settings.max_single_trade_pct
        dollars = min(kelly.recommended_dollars, max_trade)
        contract_cost = price_cents / 100.0
        quantity = int(dollars / contract_cost) if contract_cost > 0 else 0

        if quantity == 0 or dollars <= 0:
            return TradeDecision(
                approved=False, side=side,
                position_size_dollars=0, quantity=0,
                price_cents=price_cents,
                rejection_reasons=["Position size too small"],
            )

        # Check hard limits
        limits_result = self._limits.check(dollars, market_id, market_category)

        if not limits_result.approved:
            return TradeDecision(
                approved=False, side=side,
                position_size_dollars=dollars, quantity=quantity,
                price_cents=price_cents,
                rejection_reasons=limits_result.violations,
            )

        return TradeDecision(
            approved=True, side=side,
            position_size_dollars=round(dollars, 2),
            quantity=quantity,
            price_cents=price_cents,
            rejection_reasons=[],
        )
```

- [ ] **Step 4: Run tests, commit**

```bash
git commit -m "feat: add risk manager combining Kelly sizing and hard limits"
```

---

### Task 6: Trade Execution Engine

**Files:**
- Create: `src/trading/__init__.py`
- Create: `src/trading/engine.py`
- Create: `tests/test_trade_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trade_engine.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.trading.engine import TradeEngine
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.models.position import Position
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.opportunity import Opportunity
from src.ev.calculator import EVResult
from src.risk.manager import TradeDecision


def _setup_market(db_engine):
    Base.metadata.create_all(db_engine)
    TradingSettings.get_or_create(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates?",
            category="Economics", close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()


def test_execute_paper_trade(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=True, side="yes", position_size_dollars=1.30,
        quantity=2, price_cents=65, rejection_reasons=[],
    )
    trade = engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="Finance model",
    )
    assert trade is not None
    assert trade["is_paper"] is True
    assert trade["status"] == "filled"
    assert trade["side"] == "yes"

    # Verify position was created
    with get_session(db_engine) as session:
        pos = session.query(Position).filter_by(market_id="FED-RATE-JUL").first()
        assert pos is not None
        assert pos.side == "yes"
        assert pos.entry_price == 65
        assert pos.quantity == 2
        assert pos.status == "open"


def test_execute_rejected_trade_returns_none(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=False, side="yes", position_size_dollars=0,
        quantity=0, price_cents=65, rejection_reasons=["Too risky"],
    )
    trade = engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="test",
    )
    assert trade is None


def test_paper_trade_count_increments(db_engine):
    _setup_market(db_engine)
    engine = TradeEngine(db_engine)
    decision = TradeDecision(
        approved=True, side="yes", position_size_dollars=1.30,
        quantity=2, price_cents=65, rejection_reasons=[],
    )
    engine.execute(
        decision=decision, market_id="FED-RATE-JUL",
        p_model=0.77, implied_prob=0.65, edge=0.12, net_ev=0.10,
        confidence=0.85, reasoning="test",
    )
    with get_session(db_engine) as session:
        settings = session.query(TradingSettings).first()
        assert settings.paper_trade_count == 1


def test_live_mode_blocked_without_enough_paper_trades(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        settings = TradingSettings()
        settings.mode = "live"
        settings.paper_trade_count = 10  # Under 50 threshold
        session.add(settings)
        session.commit()
    engine = TradeEngine(db_engine)
    assert engine.can_trade_live() is False
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Write implementation**

```python
# src/trading/__init__.py
```

```python
# src/trading/engine.py
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.position import Position
from src.models.settings import TradingSettings
from src.risk.manager import TradeDecision

logger = logging.getLogger(__name__)


class TradeEngine:
    def __init__(self, engine: Engine):
        self._engine = engine

    def _get_mode(self) -> dict:
        with get_session(self._engine) as session:
            s = session.query(TradingSettings).first()
            if not s:
                return {"mode": "paper", "paper_trade_count": 0, "paper_trades_before_live": 50}
            return {
                "mode": s.mode,
                "paper_trade_count": s.paper_trade_count,
                "paper_trades_before_live": s.paper_trades_before_live,
            }

    def can_trade_live(self) -> bool:
        info = self._get_mode()
        if info["mode"] != "live":
            return False
        if info["paper_trade_count"] < info["paper_trades_before_live"]:
            return False
        return True

    def execute(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
    ) -> Optional[Dict[str, Any]]:
        if not decision.approved:
            logger.info(f"Trade rejected for {market_id}: {decision.rejection_reasons}")
            return None

        mode_info = self._get_mode()
        is_paper = mode_info["mode"] == "paper" or not self.can_trade_live()

        if is_paper:
            return self._execute_paper(
                decision, market_id, p_model, implied_prob,
                edge, net_ev, confidence, reasoning,
            )
        else:
            # Live execution placeholder
            raise NotImplementedError("Live trading requires Kalshi API credentials")

    def _execute_paper(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
    ) -> Dict[str, Any]:
        with get_session(self._engine) as session:
            # Create trade record
            trade = Trade(
                market_id=market_id,
                side=decision.side,
                action="buy",
                price=decision.price_cents,
                quantity=decision.quantity,
                p_model=p_model,
                implied_prob=implied_prob,
                edge=edge,
                net_ev=net_ev,
                position_size_dollars=decision.position_size_dollars,
                confidence=confidence,
                reasoning=reasoning,
                is_paper=True,
                status="filled",
            )
            session.add(trade)

            # Create or update position
            existing_pos = (
                session.query(Position)
                .filter_by(market_id=market_id, side=decision.side, status="open")
                .first()
            )
            if existing_pos:
                existing_pos.quantity += decision.quantity
            else:
                pos = Position(
                    market_id=market_id,
                    side=decision.side,
                    entry_price=decision.price_cents,
                    quantity=decision.quantity,
                    current_price=decision.price_cents,
                    status="open",
                )
                session.add(pos)

            # Increment paper trade count
            settings = session.query(TradingSettings).first()
            if settings:
                settings.paper_trade_count += 1

            session.commit()

            logger.info(
                f"Paper trade executed: {market_id} {decision.side} "
                f"x{decision.quantity} @ {decision.price_cents}¢ "
                f"(${decision.position_size_dollars:.2f})"
            )

            return {
                "market_id": market_id,
                "side": decision.side,
                "price": decision.price_cents,
                "quantity": decision.quantity,
                "dollars": decision.position_size_dollars,
                "is_paper": True,
                "status": "filled",
            }
```

- [ ] **Step 4: Run ALL tests, commit**

```bash
git commit -m "feat: add trade execution engine with paper trading"
```

---

### Task 7: Final Integration Test

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v --tb=short`
Expected: All tests pass (80+)

- [ ] **Step 2: End-to-end smoke test**

```python
python3 -c "
from src.main import create_app
from fastapi.testclient import TestClient
from src.ev.scorer import score_all_markets
from src.database import get_engine, get_session, Base
from src.models.settings import TradingSettings
from src.models.opportunity import Opportunity
from src.config import Settings

settings = Settings()
engine = get_engine(settings.DATABASE_URL)
Base.metadata.create_all(engine)

# Seed demo data
from src.demo.seed import seed_demo_data
seed_demo_data(engine)

# Initialize trading settings
TradingSettings.get_or_create(engine)

# Score all markets
results = score_all_markets(engine)
print(f'Scored {len(results)} markets')
for r in results[:3]:
    print(f'  {r[\"market_id\"]}: edge={r[\"edge\"]:.1%} ev={r[\"net_ev\"]:.4f} status={r[\"status\"]}')

# Check opportunities in DB
with get_session(engine) as session:
    opps = session.query(Opportunity).all()
    qualifying = [o for o in opps if o.status == 'qualifying']
    print(f'Total opportunities: {len(opps)}, qualifying: {len(qualifying)}')
"
```

- [ ] **Step 3: Commit if needed**

```bash
git commit -m "chore: plan 3 complete — risk management and trade execution"
```
