# Plan 2: Probability Modeling + EV Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the probability modeling layer and EV calculation engine that identifies positive expected value trading opportunities from Kalshi market data.

**Architecture:** Pluggable model system with a common interface. Each market category (sports, finance, etc.) gets its own model plugin. The EV engine scores every market, and the trade filter surfaces only qualifying opportunities. All results stored in SQLite.

**Tech Stack:** Python 3.9, SQLAlchemy, pydantic, scipy (for statistics)

---

## File Structure

```
src/
├── modeling/
│   ├── __init__.py
│   ├── base.py              # Abstract model interface
│   ├── confidence.py         # Confidence score calculation
│   ├── registry.py           # Model registry (maps categories to models)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── consensus.py      # Fallback: wisdom-of-crowds / spread-based model
│   │   ├── finance.py        # Finance/economics model (historical base rates)
│   │   └── sports.py         # Sports model (odds comparison)
├── ev/
│   ├── __init__.py
│   ├── calculator.py         # EV calculation engine
│   └── filter.py             # Trade filtering (all conditions)
├── models/
│   ├── opportunity.py        # Opportunity SQLAlchemy model (stores scored markets)
tests/
├── test_modeling_base.py
├── test_consensus_model.py
├── test_finance_model.py
├── test_sports_model.py
├── test_confidence.py
├── test_ev_calculator.py
├── test_ev_filter.py
├── test_opportunity_model.py
```

---

### Task 1: Model Interface + Confidence Score

**Files:**
- Create: `src/modeling/__init__.py`
- Create: `src/modeling/base.py`
- Create: `src/modeling/confidence.py`
- Create: `tests/test_modeling_base.py`
- Create: `tests/test_confidence.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_modeling_base.py
from src.modeling.base import ModelResult


def test_model_result_creation():
    result = ModelResult(
        market_id="FED-RATE-JUL",
        p_model=0.77,
        confidence=0.85,
        reasoning="Historical base rate for Fed holds is 77%",
        data_sources=["FRED", "Fed minutes"],
    )
    assert result.market_id == "FED-RATE-JUL"
    assert result.p_model == 0.77
    assert result.confidence == 0.85
    assert len(result.data_sources) == 2


def test_model_result_clamps_probability():
    result = ModelResult(market_id="X", p_model=1.5, confidence=0.5, reasoning="test")
    assert result.p_model == 1.0

    result2 = ModelResult(market_id="X", p_model=-0.1, confidence=0.5, reasoning="test")
    assert result2.p_model == 0.0
```

```python
# tests/test_confidence.py
from src.modeling.confidence import ConfidenceScore


def test_high_confidence():
    score = ConfidenceScore(
        data_freshness=0.9,
        data_completeness=0.8,
        historical_calibration=0.85,
    )
    assert score.overall >= 0.7
    assert score.tier == "high"


def test_medium_confidence():
    score = ConfidenceScore(
        data_freshness=0.6,
        data_completeness=0.5,
        historical_calibration=0.5,
    )
    assert 0.4 <= score.overall < 0.7
    assert score.tier == "medium"


def test_low_confidence():
    score = ConfidenceScore(
        data_freshness=0.2,
        data_completeness=0.3,
        historical_calibration=0.2,
    )
    assert score.overall < 0.4
    assert score.tier == "low"


def test_edge_threshold_by_tier():
    high = ConfidenceScore(data_freshness=0.9, data_completeness=0.9, historical_calibration=0.9)
    medium = ConfidenceScore(data_freshness=0.5, data_completeness=0.5, historical_calibration=0.5)
    low = ConfidenceScore(data_freshness=0.1, data_completeness=0.1, historical_calibration=0.1)

    assert high.edge_threshold == 0.05
    assert medium.edge_threshold == 0.08
    assert low.edge_threshold == 0.12
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_modeling_base.py tests/test_confidence.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Write ModelResult**

```python
# src/modeling/__init__.py
```

```python
# src/modeling/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import Engine


@dataclass
class ModelResult:
    market_id: str
    p_model: float
    confidence: float
    reasoning: str
    data_sources: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.p_model = max(0.0, min(1.0, self.p_model))
        self.confidence = max(0.0, min(1.0, self.confidence))


class BaseModel(ABC):
    """Abstract base class for probability models."""

    @property
    @abstractmethod
    def category(self) -> str:
        """Market category this model handles (e.g., 'Economics', 'Sports')."""
        ...

    @abstractmethod
    def estimate(self, market_id: str, title: str, current_price: int, engine: Engine) -> Optional[ModelResult]:
        """Estimate true probability for a market. Returns None if model can't handle this market."""
        ...
```

- [ ] **Step 4: Write ConfidenceScore**

```python
# src/modeling/confidence.py
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ConfidenceScore:
    data_freshness: float      # 0-1: how recent the external data is
    data_completeness: float   # 0-1: are all expected inputs available?
    historical_calibration: float  # 0-1: how well model has performed historically

    @property
    def overall(self) -> float:
        weighted = (
            self.data_freshness * 0.3
            + self.data_completeness * 0.3
            + self.historical_calibration * 0.4
        )
        return max(0.0, min(1.0, weighted))

    @property
    def tier(self) -> str:
        score = self.overall
        if score >= 0.7:
            return "high"
        elif score >= 0.4:
            return "medium"
        else:
            return "low"

    @property
    def edge_threshold(self) -> float:
        tier = self.tier
        if tier == "high":
            return 0.05
        elif tier == "medium":
            return 0.08
        else:
            return 0.12
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_modeling_base.py tests/test_confidence.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/modeling/__init__.py src/modeling/base.py src/modeling/confidence.py tests/test_modeling_base.py tests/test_confidence.py
git commit -m "feat: add model interface and confidence scoring"
```

---

### Task 2: Consensus (Fallback) Model

**Files:**
- Create: `src/modeling/models/__init__.py`
- Create: `src/modeling/models/consensus.py`
- Create: `tests/test_consensus_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consensus_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.modeling.models.consensus import ConsensusModel
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_consensus_model_estimates_from_spread(db_engine):
    """When bid/ask spread is tight, model uses midpoint as probability estimate
    with a small correction based on spread width."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="TEST-MKT", title="Test Market", category="General",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        # Add several price snapshots showing consistent pricing
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="TEST-MKT", yes_bid=65, yes_ask=67, last_price=66,
                volume=2000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = ConsensusModel()
    result = model.estimate("TEST-MKT", "Test Market", current_price=66, engine=db_engine)

    assert result is not None
    assert result.market_id == "TEST-MKT"
    # Midpoint of 65/67 = 66 cents = 0.66 implied prob. Model should be close.
    assert 0.60 <= result.p_model <= 0.72
    assert result.confidence > 0


def test_consensus_model_returns_none_with_no_data(db_engine):
    """Model returns None when there's insufficient price history."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="EMPTY-MKT", title="No Data", category="General",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        session.commit()

    model = ConsensusModel()
    result = model.estimate("EMPTY-MKT", "No Data", current_price=50, engine=db_engine)
    assert result is None


def test_consensus_model_detects_price_trend(db_engine):
    """When recent prices show a trend away from current price, model adjusts."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="TREND-MKT", title="Trending Market", category="General",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        # Prices trending upward: 50 -> 60 -> 70
        for i, price in enumerate([50, 52, 55, 58, 60, 62, 65, 67, 69, 70]):
            session.add(PriceSnapshot(
                market_id="TREND-MKT", yes_bid=price, yes_ask=price + 2,
                last_price=price + 1, volume=1000,
                timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = ConsensusModel()
    # Current price is 70, but trend suggests momentum
    result = model.estimate("TREND-MKT", "Trending Market", current_price=70, engine=db_engine)
    assert result is not None
    # Model should estimate slightly above current implied prob due to uptrend
    assert result.p_model >= 0.70


def test_consensus_handles_any_category():
    model = ConsensusModel()
    assert model.category == "fallback"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_consensus_model.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/modeling/models/__init__.py
```

```python
# src/modeling/models/consensus.py
from __future__ import annotations
import logging
from typing import Optional, List

from sqlalchemy import Engine

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.price import PriceSnapshot

logger = logging.getLogger(__name__)

MIN_SNAPSHOTS = 5  # Minimum price history needed


class ConsensusModel(BaseModel):
    """Fallback model that estimates probability from price history and spread analysis.

    Works for any market category. Uses:
    - Bid/ask midpoint as base probability
    - Spread width as uncertainty indicator
    - Price trend as directional adjustment
    """

    @property
    def category(self) -> str:
        return "fallback"

    def estimate(self, market_id: str, title: str, current_price: int, engine: Engine) -> Optional[ModelResult]:
        with get_session(engine) as session:
            snapshots = (
                session.query(PriceSnapshot)
                .filter_by(market_id=market_id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(50)
                .all()
            )

        if len(snapshots) < MIN_SNAPSHOTS:
            return None

        # Calculate base probability from recent midpoints
        recent = snapshots[:10]
        midpoints = [(s.yes_bid + s.yes_ask) / 2.0 for s in recent]
        avg_midpoint = sum(midpoints) / len(midpoints)

        # Calculate spread (tighter spread = more confidence)
        spreads = [s.yes_ask - s.yes_bid for s in recent]
        avg_spread = sum(spreads) / len(spreads)

        # Detect trend from price history
        if len(snapshots) >= 10:
            old_prices = [s.last_price for s in snapshots[-5:]]
            new_prices = [s.last_price for s in snapshots[:5]]
            old_avg = sum(old_prices) / len(old_prices)
            new_avg = sum(new_prices) / len(new_prices)
            trend = (new_avg - old_avg) / 100.0  # Normalize to probability scale
        else:
            trend = 0.0

        # Base probability from midpoint + trend adjustment
        p_model = (avg_midpoint / 100.0) + (trend * 0.1)
        p_model = max(0.01, min(0.99, p_model))

        # Confidence based on data quality
        data_freshness = min(1.0, len(recent) / 10.0)
        data_completeness = 1.0 if avg_spread <= 5 else max(0.3, 1.0 - (avg_spread - 5) * 0.1)
        # Fallback model has low historical calibration by default
        historical_calibration = 0.4

        confidence = ConfidenceScore(
            data_freshness=data_freshness,
            data_completeness=data_completeness,
            historical_calibration=historical_calibration,
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=confidence.overall,
            reasoning=f"Consensus from price history: midpoint={avg_midpoint:.0f}¢, spread={avg_spread:.1f}¢, trend={trend:+.3f}",
            data_sources=["price_history"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_consensus_model.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/modeling/models/ tests/test_consensus_model.py
git commit -m "feat: add consensus fallback probability model"
```

---

### Task 3: Finance Model

**Files:**
- Create: `src/modeling/models/finance.py`
- Create: `tests/test_finance_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_finance_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.modeling.models.finance import FinanceModel
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_finance_model_category():
    model = FinanceModel()
    assert model.category == "Economics"


def test_finance_model_estimates_fed_market(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates in July?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = FinanceModel()
    result = model.estimate("FED-RATE-JUL", "Will Fed raise rates in July?", current_price=66, engine=db_engine)

    assert result is not None
    assert result.market_id == "FED-RATE-JUL"
    assert 0.0 < result.p_model < 1.0
    assert result.confidence > 0
    assert "Economics" in result.data_sources or "historical_rates" in result.data_sources


def test_finance_model_skips_non_finance(db_engine):
    """Finance model returns None for non-economics markets."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="LAKERS-WIN", title="Will Lakers win?",
            category="Sports",
            close_date=datetime(2026, 4, 25, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="LAKERS-WIN", yes_bid=45, yes_ask=47, last_price=46,
                volume=1000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = FinanceModel()
    result = model.estimate("LAKERS-WIN", "Will Lakers win?", current_price=46, engine=db_engine)
    assert result is None


def test_finance_model_adjusts_for_keywords(db_engine):
    """Model should detect keywords like 'CPI', 'GDP', 'Fed' and apply historical priors."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="CPI-UNDER-3", title="Will CPI be under 3% in June?",
            category="Economics",
            close_date=datetime(2026, 6, 30, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="CPI-UNDER-3", yes_bid=70, yes_ask=72, last_price=71,
                volume=3000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = FinanceModel()
    result = model.estimate("CPI-UNDER-3", "Will CPI be under 3% in June?", current_price=71, engine=db_engine)
    assert result is not None
    assert "CPI" in result.reasoning or "inflation" in result.reasoning.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_finance_model.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/modeling/models/finance.py
from __future__ import annotations
import logging
import re
from typing import Optional, Dict

from sqlalchemy import Engine

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.market import Market
from src.models.price import PriceSnapshot

logger = logging.getLogger(__name__)

# Historical base rates for common economic events (simplified priors)
KEYWORD_PRIORS: Dict[str, float] = {
    "fed": 0.50,          # Fed decisions are roughly 50/50 historically
    "rate": 0.45,         # Rate changes slightly less common than holds
    "cpi": 0.55,          # CPI thresholds met slightly more often than not
    "inflation": 0.55,
    "gdp": 0.50,          # GDP thresholds are roughly coin-flip
    "unemployment": 0.50,
    "jobs": 0.50,
    "recession": 0.20,    # Recessions are relatively rare
    "s&p": 0.55,          # Markets tend to go up over time
    "sp500": 0.55,
    "nasdaq": 0.55,
    "bitcoin": 0.50,
    "btc": 0.50,
}


class FinanceModel(BaseModel):
    """Probability model for economics/finance markets.

    Uses:
    - Historical base rates for economic events (keyword-based priors)
    - Price history from the market itself
    - Blends prior with market consensus, weighted by confidence
    """

    @property
    def category(self) -> str:
        return "Economics"

    def estimate(self, market_id: str, title: str, current_price: int, engine: Engine) -> Optional[ModelResult]:
        # Check if this is a finance market
        with get_session(engine) as session:
            market = session.query(Market).filter_by(market_id=market_id).first()
            if not market or market.category != "Economics":
                return None

            snapshots = (
                session.query(PriceSnapshot)
                .filter_by(market_id=market_id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(50)
                .all()
            )

        if len(snapshots) < 5:
            return None

        # Get historical prior from keywords in title
        title_lower = title.lower()
        prior = None
        matched_keyword = None
        for keyword, base_rate in KEYWORD_PRIORS.items():
            if keyword in title_lower:
                prior = base_rate
                matched_keyword = keyword
                break

        # Market consensus from recent prices
        recent = snapshots[:10]
        midpoints = [(s.yes_bid + s.yes_ask) / 2.0 for s in recent]
        market_consensus = sum(midpoints) / len(midpoints) / 100.0

        # Blend prior with market consensus
        if prior is not None:
            # Weight: 30% prior, 70% market (market is usually more informed)
            p_model = prior * 0.3 + market_consensus * 0.7
            reasoning = f"Finance model: {matched_keyword} prior={prior:.0%}, market consensus={market_consensus:.0%}, blended={p_model:.0%}"
        else:
            # No keyword match — use consensus with small adjustment
            p_model = market_consensus
            reasoning = f"Finance model: no keyword prior, using market consensus={market_consensus:.0%}"

        p_model = max(0.01, min(0.99, p_model))

        # Volume-based confidence
        avg_volume = sum(s.volume for s in recent) / len(recent)
        vol_score = min(1.0, avg_volume / 3000.0)

        confidence = ConfidenceScore(
            data_freshness=0.7,  # No external API yet, using market data only
            data_completeness=0.6 if prior is not None else 0.4,
            historical_calibration=0.5,
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=confidence.overall,
            reasoning=reasoning,
            data_sources=["historical_rates", "price_history"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_finance_model.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/modeling/models/finance.py tests/test_finance_model.py
git commit -m "feat: add finance probability model with keyword priors"
```

---

### Task 4: Sports Model

**Files:**
- Create: `src/modeling/models/sports.py`
- Create: `tests/test_sports_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sports_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.modeling.models.sports import SportsModel
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_sports_model_category():
    model = SportsModel()
    assert model.category == "Sports"


def test_sports_model_estimates_game_market(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="LAKERS-WIN", title="Will Lakers win tonight?",
            category="Sports",
            close_date=datetime(2026, 4, 25, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="LAKERS-WIN", yes_bid=42, yes_ask=44, last_price=43,
                volume=2000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = SportsModel()
    result = model.estimate("LAKERS-WIN", "Will Lakers win tonight?", current_price=43, engine=db_engine)

    assert result is not None
    assert result.market_id == "LAKERS-WIN"
    assert 0.0 < result.p_model < 1.0
    assert result.confidence > 0


def test_sports_model_skips_non_sports(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE", title="Will Fed raise rates?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = SportsModel()
    result = model.estimate("FED-RATE", "Will Fed raise rates?", current_price=66, engine=db_engine)
    assert result is None


def test_sports_model_adjusts_for_volatility(db_engine):
    """High price volatility should reduce confidence."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="VOLATILE-GAME", title="Will Team X win?",
            category="Sports",
            close_date=datetime(2026, 4, 25, tzinfo=timezone.utc), status="open",
        ))
        # Highly volatile prices
        for i, price in enumerate([30, 50, 35, 55, 40, 60, 45, 65, 50, 70]):
            session.add(PriceSnapshot(
                market_id="VOLATILE-GAME", yes_bid=price, yes_ask=price + 2,
                last_price=price + 1, volume=500,
                timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    model = SportsModel()
    result = model.estimate("VOLATILE-GAME", "Will Team X win?", current_price=70, engine=db_engine)
    assert result is not None
    # High volatility should mean lower confidence
    assert result.confidence < 0.6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_sports_model.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/modeling/models/sports.py
from __future__ import annotations
import logging
import math
from typing import Optional

from sqlalchemy import Engine

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.market import Market
from src.models.price import PriceSnapshot

logger = logging.getLogger(__name__)


class SportsModel(BaseModel):
    """Probability model for sports markets.

    Uses:
    - Market price consensus as base
    - Price volatility to assess uncertainty
    - Volume as liquidity signal
    Without external sports APIs (future enhancement), relies on
    market microstructure signals.
    """

    @property
    def category(self) -> str:
        return "Sports"

    def estimate(self, market_id: str, title: str, current_price: int, engine: Engine) -> Optional[ModelResult]:
        with get_session(engine) as session:
            market = session.query(Market).filter_by(market_id=market_id).first()
            if not market or market.category != "Sports":
                return None

            snapshots = (
                session.query(PriceSnapshot)
                .filter_by(market_id=market_id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(50)
                .all()
            )

        if len(snapshots) < 5:
            return None

        recent = snapshots[:10]

        # Base probability from price midpoints
        midpoints = [(s.yes_bid + s.yes_ask) / 2.0 for s in recent]
        avg_midpoint = sum(midpoints) / len(midpoints)
        p_base = avg_midpoint / 100.0

        # Volatility: standard deviation of midpoints
        variance = sum((m - avg_midpoint) ** 2 for m in midpoints) / len(midpoints)
        volatility = math.sqrt(variance)

        # Volume signal
        avg_volume = sum(s.volume for s in recent) / len(recent)

        # Adjust probability: high volatility means less certainty in direction
        # Use a slight mean-reversion adjustment for very extreme prices
        if p_base > 0.8:
            p_model = p_base - 0.02  # Slight correction for overconfidence
        elif p_base < 0.2:
            p_model = p_base + 0.02
        else:
            p_model = p_base

        p_model = max(0.01, min(0.99, p_model))

        # Confidence based on volatility and volume
        vol_confidence = max(0.2, 1.0 - (volatility / 20.0))  # High vol = low confidence
        volume_confidence = min(1.0, avg_volume / 2000.0)

        confidence = ConfidenceScore(
            data_freshness=0.7,
            data_completeness=0.5,  # No external sports API yet
            historical_calibration=vol_confidence,
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=confidence.overall,
            reasoning=f"Sports model: consensus={p_base:.0%}, volatility={volatility:.1f}¢, avg_volume={avg_volume:.0f}",
            data_sources=["price_history"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_sports_model.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/modeling/models/sports.py tests/test_sports_model.py
git commit -m "feat: add sports probability model"
```

---

### Task 5: Model Registry

**Files:**
- Create: `src/modeling/registry.py`
- Modify: `tests/test_modeling_base.py` (add registry tests)

- [ ] **Step 1: Write the failing test**

Add to a new test file:

```python
# tests/test_model_registry.py
from src.modeling.registry import ModelRegistry
from src.modeling.models.consensus import ConsensusModel
from src.modeling.models.finance import FinanceModel
from src.modeling.models.sports import SportsModel


def test_registry_returns_category_model():
    registry = ModelRegistry()
    models = registry.get_models_for("Economics")
    categories = [m.category for m in models]
    assert "Economics" in categories


def test_registry_always_includes_fallback():
    registry = ModelRegistry()
    models = registry.get_models_for("UnknownCategory")
    categories = [m.category for m in models]
    assert "fallback" in categories


def test_registry_returns_specialized_plus_fallback():
    registry = ModelRegistry()
    models = registry.get_models_for("Sports")
    categories = [m.category for m in models]
    assert "Sports" in categories
    assert "fallback" in categories


def test_registry_all_models():
    registry = ModelRegistry()
    all_models = registry.all_models
    assert len(all_models) >= 3  # finance, sports, consensus
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_model_registry.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/modeling/registry.py
from __future__ import annotations
from typing import List

from src.modeling.base import BaseModel
from src.modeling.models.consensus import ConsensusModel
from src.modeling.models.finance import FinanceModel
from src.modeling.models.sports import SportsModel


class ModelRegistry:
    """Registry mapping market categories to probability models.

    Each category gets its specialized model(s) plus the consensus fallback.
    """

    def __init__(self):
        self._fallback = ConsensusModel()
        self._models: List[BaseModel] = [
            FinanceModel(),
            SportsModel(),
        ]

    @property
    def all_models(self) -> List[BaseModel]:
        return self._models + [self._fallback]

    def get_models_for(self, category: str) -> List[BaseModel]:
        """Return models applicable to a market category.
        Specialized models first, fallback last."""
        specialized = [m for m in self._models if m.category == category]
        return specialized + [self._fallback]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_model_registry.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/modeling/registry.py tests/test_model_registry.py
git commit -m "feat: add model registry for category-based model dispatch"
```

---

### Task 6: EV Calculator

**Files:**
- Create: `src/ev/__init__.py`
- Create: `src/ev/calculator.py`
- Create: `tests/test_ev_calculator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ev_calculator.py
from src.ev.calculator import calculate_ev, EVResult


def test_positive_ev():
    """When model probability > implied probability, EV is positive."""
    result = calculate_ev(
        p_model=0.77,
        price_cents=65,  # Implied prob = 65%
        fee_rate=0.01,
    )
    assert result.raw_ev > 0
    assert result.net_ev > 0
    assert result.edge > 0
    assert result.edge == 0.77 - 0.65


def test_negative_ev():
    """When model probability < implied probability, EV is negative."""
    result = calculate_ev(
        p_model=0.50,
        price_cents=65,
        fee_rate=0.01,
    )
    assert result.raw_ev < 0
    assert result.net_ev < 0
    assert result.edge < 0


def test_ev_formula():
    """Verify the EV formula: EV = p * (1 - price) - (1-p) * price"""
    result = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.0)
    price = 0.65
    expected_ev = 0.77 * (1 - price) - (1 - 0.77) * price
    assert abs(result.raw_ev - expected_ev) < 0.001


def test_fees_reduce_ev():
    no_fee = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.0)
    with_fee = calculate_ev(p_model=0.77, price_cents=65, fee_rate=0.05)
    assert with_fee.net_ev < no_fee.net_ev


def test_ev_for_no_position():
    """Test EV calculation for taking the NO side (buying No contract)."""
    result = calculate_ev(
        p_model=0.30,  # Model thinks 30% chance of Yes
        price_cents=45,  # Market says 45% Yes
        fee_rate=0.01,
    )
    # Edge on NO side: (1 - 0.30) - (1 - 0.45) = 0.70 - 0.55 = 0.15
    # This is the same as the Yes edge but inverted
    assert result.edge == 0.30 - 0.45  # Negative edge on Yes
    assert result.no_edge == (1 - 0.30) - (1 - 0.45)  # Positive edge on No
    assert result.no_ev > 0  # No side is profitable
    assert result.recommended_side == "no"


def test_recommended_side_yes():
    result = calculate_ev(p_model=0.80, price_cents=65, fee_rate=0.01)
    assert result.recommended_side == "yes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_ev_calculator.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/ev/__init__.py
```

```python
# src/ev/calculator.py
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class EVResult:
    p_model: float
    implied_prob: float
    edge: float          # p_model - implied_prob (Yes side)
    no_edge: float       # (1 - p_model) - (1 - implied_prob) (No side)
    raw_ev: float        # EV before fees (Yes side)
    net_ev: float        # EV after fees (Yes side)
    no_ev: float         # EV on No side (after fees)
    recommended_side: str  # "yes" or "no"
    fee_rate: float

    @property
    def best_edge(self) -> float:
        return self.edge if self.recommended_side == "yes" else self.no_edge

    @property
    def best_ev(self) -> float:
        return self.net_ev if self.recommended_side == "yes" else self.no_ev


def calculate_ev(p_model: float, price_cents: int, fee_rate: float = 0.01) -> EVResult:
    """Calculate expected value for a Kalshi contract.

    Args:
        p_model: Model's estimated true probability (0-1)
        price_cents: Current price in cents (1-99), also the implied probability
        fee_rate: Fee as fraction of payout (e.g., 0.01 = 1%)

    Returns:
        EVResult with EV calculations for both Yes and No sides
    """
    price = price_cents / 100.0
    implied_prob = price

    # Yes side: pay `price`, win `1 - price` if correct
    # EV = p * (1 - price) - (1 - p) * price
    raw_ev_yes = p_model * (1 - price) - (1 - p_model) * price
    fee_cost = fee_rate  # Fee on the payout
    net_ev_yes = raw_ev_yes - fee_cost

    # No side: pay `1 - price`, win `price` if correct
    # EV = (1-p) * price - p * (1 - price)
    raw_ev_no = (1 - p_model) * price - p_model * (1 - price)
    net_ev_no = raw_ev_no - fee_cost

    edge_yes = p_model - implied_prob
    edge_no = (1 - p_model) - (1 - implied_prob)

    # Recommend whichever side has better EV
    if net_ev_yes >= net_ev_no:
        recommended_side = "yes"
    else:
        recommended_side = "no"

    return EVResult(
        p_model=p_model,
        implied_prob=implied_prob,
        edge=edge_yes,
        no_edge=edge_no,
        raw_ev=raw_ev_yes,
        net_ev=net_ev_yes,
        no_ev=net_ev_no,
        recommended_side=recommended_side,
        fee_rate=fee_rate,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_ev_calculator.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/ev/ tests/test_ev_calculator.py
git commit -m "feat: add EV calculator with Yes/No side analysis"
```

---

### Task 7: Trade Filter

**Files:**
- Create: `src/ev/filter.py`
- Create: `tests/test_ev_filter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ev_filter.py
from src.ev.calculator import EVResult
from src.ev.filter import TradeFilter, FilterResult


def test_qualifies_good_trade():
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    result = f.evaluate(
        ev_result=ev,
        confidence=0.8,
        daily_volume=5000,
        bid_ask_spread_cents=2,
        hours_to_expiry=48,
    )
    assert result.qualifies is True
    assert len(result.rejection_reasons) == 0


def test_rejects_low_edge():
    ev = EVResult(
        p_model=0.67, implied_prob=0.65, edge=0.02, no_edge=-0.02,
        raw_ev=0.02, net_ev=0.01, no_ev=-0.03,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    result = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                        bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("edge" in r.lower() for r in result.rejection_reasons)


def test_rejects_negative_ev():
    ev = EVResult(
        p_model=0.50, implied_prob=0.65, edge=-0.15, no_edge=0.15,
        raw_ev=-0.15, net_ev=-0.16, no_ev=0.14,
        recommended_side="no", fee_rate=0.01,
    )
    f = TradeFilter()
    # Even though No side has positive EV, test with the best side
    result = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                        bid_ask_spread_cents=2, hours_to_expiry=48)
    # No side edge is 0.15, which exceeds threshold — should qualify
    assert result.qualifies is True


def test_rejects_low_volume():
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    result = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=100,
                        bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("volume" in r.lower() or "liquidity" in r.lower() for r in result.rejection_reasons)


def test_rejects_wide_spread():
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    result = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                        bid_ask_spread_cents=8, hours_to_expiry=48)
    assert result.qualifies is False
    assert any("spread" in r.lower() for r in result.rejection_reasons)


def test_rejects_expiring_soon():
    ev = EVResult(
        p_model=0.77, implied_prob=0.65, edge=0.12, no_edge=-0.12,
        raw_ev=0.12, net_ev=0.10, no_ev=-0.14,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    result = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                        bid_ask_spread_cents=2, hours_to_expiry=0.5)
    assert result.qualifies is False
    assert any("expir" in r.lower() for r in result.rejection_reasons)


def test_medium_confidence_raises_threshold():
    """Medium confidence requires edge > 8% instead of 5%."""
    ev = EVResult(
        p_model=0.71, implied_prob=0.65, edge=0.06, no_edge=-0.06,
        raw_ev=0.06, net_ev=0.04, no_ev=-0.08,
        recommended_side="yes", fee_rate=0.01,
    )
    f = TradeFilter()
    # 6% edge with high confidence: passes
    result_high = f.evaluate(ev_result=ev, confidence=0.8, daily_volume=5000,
                             bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result_high.qualifies is True

    # 6% edge with medium confidence: fails (threshold is 8%)
    result_med = f.evaluate(ev_result=ev, confidence=0.5, daily_volume=5000,
                            bid_ask_spread_cents=2, hours_to_expiry=48)
    assert result_med.qualifies is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_ev_filter.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/ev/filter.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

from src.ev.calculator import EVResult
from src.modeling.confidence import ConfidenceScore


@dataclass
class FilterResult:
    qualifies: bool
    rejection_reasons: List[str] = field(default_factory=list)
    status: str = "qualifying"  # "qualifying", "watching", "rejected"


class TradeFilter:
    """Filter trade opportunities based on strict criteria.

    ALL conditions must pass for a trade to qualify.
    """

    def __init__(
        self,
        min_daily_volume: int = 500,
        max_spread_cents: int = 5,
        min_hours_to_expiry: float = 1.0,
    ):
        self.min_daily_volume = min_daily_volume
        self.max_spread_cents = max_spread_cents
        self.min_hours_to_expiry = min_hours_to_expiry

    def _get_edge_threshold(self, confidence: float) -> float:
        """Dynamic edge threshold based on confidence level."""
        if confidence >= 0.7:
            return 0.05  # 5% for high confidence
        elif confidence >= 0.4:
            return 0.08  # 8% for medium confidence
        else:
            return 0.12  # 12% for low confidence

    def evaluate(
        self,
        ev_result: EVResult,
        confidence: float,
        daily_volume: int,
        bid_ask_spread_cents: int,
        hours_to_expiry: float,
    ) -> FilterResult:
        reasons = []

        # Use the best side (yes or no)
        best_ev = ev_result.best_ev
        best_edge = ev_result.best_edge

        # 1. Net EV must be positive
        if best_ev <= 0:
            reasons.append(f"Negative net EV: {best_ev:.4f}")

        # 2. Edge must exceed confidence-adjusted threshold
        edge_threshold = self._get_edge_threshold(confidence)
        if abs(best_edge) < edge_threshold:
            reasons.append(
                f"Edge {abs(best_edge):.1%} below threshold {edge_threshold:.0%} "
                f"(confidence={confidence:.2f})"
            )

        # 3. Sufficient volume
        if daily_volume < self.min_daily_volume:
            reasons.append(
                f"Low liquidity: volume={daily_volume} < {self.min_daily_volume}"
            )

        # 4. Acceptable spread
        if bid_ask_spread_cents > self.max_spread_cents:
            reasons.append(
                f"Wide spread: {bid_ask_spread_cents}¢ > {self.max_spread_cents}¢"
            )

        # 5. Not expiring too soon
        if hours_to_expiry < self.min_hours_to_expiry:
            reasons.append(
                f"Expiring soon: {hours_to_expiry:.1f}h < {self.min_hours_to_expiry}h"
            )

        qualifies = len(reasons) == 0

        if qualifies:
            status = "qualifying"
        elif len(reasons) == 1 and "volume" in reasons[0].lower():
            status = "watching"  # Might qualify if liquidity improves
        else:
            status = "rejected"

        return FilterResult(
            qualifies=qualifies,
            rejection_reasons=reasons,
            status=status,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_ev_filter.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/ev/filter.py tests/test_ev_filter.py
git commit -m "feat: add trade filter with confidence-adjusted thresholds"
```

---

### Task 8: Opportunity Model + Scoring Pipeline

**Files:**
- Create: `src/models/opportunity.py`
- Modify: `src/models/__init__.py`
- Create: `tests/test_opportunity_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opportunity_model.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.models.opportunity import Opportunity
from src.models.market import Market
from src.models.price import PriceSnapshot


def test_create_opportunity(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        session.commit()

        opp = Opportunity(
            market_id="FED-RATE-JUL",
            p_model=0.77,
            implied_prob=0.65,
            edge=0.12,
            net_ev=0.10,
            recommended_side="yes",
            confidence=0.85,
            status="qualifying",
            reasoning="Finance model: fed prior=50%, market=65%, blended=77%",
            model_name="FinanceModel",
        )
        session.add(opp)
        session.commit()

        fetched = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").first()
        assert fetched is not None
        assert fetched.p_model == 0.77
        assert fetched.edge == 0.12
        assert fetched.recommended_side == "yes"
        assert fetched.status == "qualifying"


def test_opportunity_updates_on_rescore(db_engine):
    """When we rescore, old opportunity gets updated, not duplicated."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        opp = Opportunity(
            market_id="FED-RATE-JUL", p_model=0.77, implied_prob=0.65,
            edge=0.12, net_ev=0.10, recommended_side="yes", confidence=0.85,
            status="qualifying", reasoning="Initial", model_name="FinanceModel",
        )
        session.add(opp)
        session.commit()

        # Update the opportunity
        existing = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").first()
        existing.p_model = 0.72
        existing.edge = 0.07
        existing.net_ev = 0.05
        existing.reasoning = "Updated estimate"
        session.commit()

        all_opps = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").all()
        assert len(all_opps) == 1
        assert all_opps[0].p_model == 0.72
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_opportunity_model.py -v`

- [ ] **Step 3: Write Opportunity model**

```python
# src/models/opportunity.py
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import String, Float, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    p_model: Mapped[float] = mapped_column(Float)
    implied_prob: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    net_ev: Mapped[float] = mapped_column(Float)
    recommended_side: Mapped[str] = mapped_column(String(10))  # "yes" or "no"
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), index=True)  # qualifying, watching, rejected
    reasoning: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(100))
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
```

- [ ] **Step 4: Update models/__init__.py**

Add `Opportunity` to the imports in `src/models/__init__.py`:

```python
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.orderbook import OrderbookSnapshot
from src.models.opportunity import Opportunity

__all__ = ["Market", "PriceSnapshot", "OrderbookSnapshot", "Opportunity"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_opportunity_model.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/models/opportunity.py src/models/__init__.py tests/test_opportunity_model.py
git commit -m "feat: add Opportunity model for storing scored markets"
```

---

### Task 9: Scoring Pipeline (ties it all together)

**Files:**
- Create: `src/ev/scorer.py`
- Create: `tests/test_scorer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scorer.py
from datetime import datetime, timezone
from src.database import get_session, Base
from src.ev.scorer import score_all_markets
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.opportunity import Opportunity


def test_score_all_markets_creates_opportunities(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        # Create a market with price history
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates in July?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    results = score_all_markets(db_engine)
    assert len(results) >= 1

    with get_session(db_engine) as session:
        opps = session.query(Opportunity).all()
        assert len(opps) >= 1
        fed_opp = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").first()
        assert fed_opp is not None
        assert fed_opp.p_model > 0


def test_score_updates_existing_opportunities(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="FED-RATE-JUL", title="Will Fed raise rates in July?",
            category="Economics",
            close_date=datetime(2026, 7, 15, tzinfo=timezone.utc), status="open",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="FED-RATE-JUL", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    score_all_markets(db_engine)
    score_all_markets(db_engine)

    with get_session(db_engine) as session:
        opps = session.query(Opportunity).filter_by(market_id="FED-RATE-JUL").all()
        assert len(opps) == 1  # Updated, not duplicated


def test_score_skips_closed_markets(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        session.add(Market(
            market_id="CLOSED-MKT", title="Closed market",
            category="Economics",
            close_date=datetime(2026, 1, 1, tzinfo=timezone.utc), status="closed",
        ))
        for i in range(10):
            session.add(PriceSnapshot(
                market_id="CLOSED-MKT", yes_bid=65, yes_ask=67, last_price=66,
                volume=5000, timestamp=datetime(2026, 4, 19, i, 0, tzinfo=timezone.utc),
            ))
        session.commit()

    results = score_all_markets(db_engine)
    with get_session(db_engine) as session:
        opp = session.query(Opportunity).filter_by(market_id="CLOSED-MKT").first()
        assert opp is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_scorer.py -v`

- [ ] **Step 3: Write implementation**

```python
# src/ev/scorer.py
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List

from sqlalchemy import Engine

from src.database import get_session
from src.ev.calculator import calculate_ev, EVResult
from src.ev.filter import TradeFilter, FilterResult
from src.modeling.base import ModelResult
from src.modeling.registry import ModelRegistry
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.models.opportunity import Opportunity

logger = logging.getLogger(__name__)


def score_all_markets(engine: Engine, fee_rate: float = 0.01) -> List[dict]:
    """Score all open markets and store opportunities in the database.

    Returns list of scoring results for each market.
    """
    registry = ModelRegistry()
    trade_filter = TradeFilter()
    results = []

    with get_session(engine) as session:
        open_markets = session.query(Market).filter_by(status="open").all()
        market_data = [
            {
                "market_id": m.market_id,
                "title": m.title,
                "category": m.category,
                "close_date": m.close_date,
            }
            for m in open_markets
        ]

    for mkt in market_data:
        # Get latest price
        with get_session(engine) as session:
            latest = (
                session.query(PriceSnapshot)
                .filter_by(market_id=mkt["market_id"])
                .order_by(PriceSnapshot.timestamp.desc())
                .first()
            )

        if not latest:
            continue

        current_price = latest.last_price
        spread = latest.yes_ask - latest.yes_bid
        volume = latest.volume

        # Get models for this category
        models = registry.get_models_for(mkt["category"])

        # Try each model, take the first that returns a result
        model_result = None
        model_name = ""
        for model in models:
            result = model.estimate(
                mkt["market_id"], mkt["title"], current_price, engine
            )
            if result is not None:
                model_result = result
                model_name = model.__class__.__name__
                break

        if model_result is None:
            continue

        # Calculate EV
        ev = calculate_ev(
            p_model=model_result.p_model,
            price_cents=current_price,
            fee_rate=fee_rate,
        )

        # Calculate hours to expiry
        now = datetime.now(timezone.utc)
        close = mkt["close_date"]
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        hours_to_expiry = max(0, (close - now).total_seconds() / 3600)

        # Filter
        filter_result = trade_filter.evaluate(
            ev_result=ev,
            confidence=model_result.confidence,
            daily_volume=volume,
            bid_ask_spread_cents=spread,
            hours_to_expiry=hours_to_expiry,
        )

        # Store opportunity (upsert)
        with get_session(engine) as session:
            existing = session.query(Opportunity).filter_by(
                market_id=mkt["market_id"]
            ).first()

            if existing:
                existing.p_model = model_result.p_model
                existing.implied_prob = ev.implied_prob
                existing.edge = ev.best_edge
                existing.net_ev = ev.best_ev
                existing.recommended_side = ev.recommended_side
                existing.confidence = model_result.confidence
                existing.status = filter_result.status
                existing.reasoning = model_result.reasoning
                existing.model_name = model_name
                existing.scored_at = datetime.now(timezone.utc)
            else:
                opp = Opportunity(
                    market_id=mkt["market_id"],
                    p_model=model_result.p_model,
                    implied_prob=ev.implied_prob,
                    edge=ev.best_edge,
                    net_ev=ev.best_ev,
                    recommended_side=ev.recommended_side,
                    confidence=model_result.confidence,
                    status=filter_result.status,
                    reasoning=model_result.reasoning,
                    model_name=model_name,
                )
                session.add(opp)
            session.commit()

        results.append({
            "market_id": mkt["market_id"],
            "p_model": model_result.p_model,
            "edge": ev.best_edge,
            "net_ev": ev.best_ev,
            "side": ev.recommended_side,
            "status": filter_result.status,
            "model": model_name,
        })

        logger.info(
            f"Scored {mkt['market_id']}: edge={ev.best_edge:.1%} "
            f"ev={ev.best_ev:.4f} status={filter_result.status}"
        )

    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/test_scorer.py -v`
Expected: 3 passed

- [ ] **Step 5: Run ALL tests**

Run: `cd "/Users/ethanperez/Desktop/kalshi dashboard" && python3 -m pytest tests/ -v`
Expected: All tests pass (35+)

- [ ] **Step 6: Commit**

```bash
git add src/ev/scorer.py tests/test_scorer.py
git commit -m "feat: add scoring pipeline that ties models, EV, and filtering together"
```
