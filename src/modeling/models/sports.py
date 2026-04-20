from __future__ import annotations

import math
from typing import List, NamedTuple, Optional

from sqlalchemy import Engine, select

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.price import PriceSnapshot

# Kalshi prices are integers in [0, 100] representing cents per dollar.
_PRICE_SCALE = 100.0

# Minimum number of snapshots required to produce an estimate.
_MIN_SNAPSHOTS = 5

# Probability thresholds for mean-reversion logic.
_HIGH_THRESHOLD = 0.80
_LOW_THRESHOLD = 0.20

# Strength of mean-reversion pull toward 0.5. A value of 0.10 means
# a price at 0.85 gets pulled 10% of the distance toward 0.5:
# reversion = 0.10 * (0.5 - 0.85) = -0.035  →  p_model = 0.815
_REVERSION_STRENGTH = 0.10

# Volatility (stdev of midpoints) that corresponds to "maximum" penalty.
# At this stdev the historical_calibration component bottoms out at 0.
_MAX_VOL = 0.25


class _SnapshotData(NamedTuple):
    yes_bid: int
    yes_ask: int


def _stdev(values: List[float]) -> float:
    """Population standard deviation of a list of floats."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


class SportsModel(BaseModel):
    """Sports-category model that estimates probability from price history.

    Algorithm
    ---------
    1. Return None if market category is not "Sports".
    2. Return None if there are fewer than 5 price snapshots.
    3. Compute the average midpoint across all snapshots as base probability.
    4. Apply slight mean-reversion when the base is extreme (>0.8 or <0.2):
       p_model = base_p + reversion_strength * (0.5 - base_p)
    5. Compute volatility as the population stdev of midpoints.
    6. Build a ConfidenceScore:
       - data_freshness         : 0.7 (fixed)
       - data_completeness      : 0.5 (fixed)
       - historical_calibration : 1.0 - (volatility / MAX_VOL), clamped to [0, 1]
         High volatility → low calibration → low overall confidence.
    """

    @property
    def category(self) -> str:
        return "Sports"

    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        category = self._load_category(market_id, engine)
        if category is None or category != "Sports":
            return None

        snapshots = self._load_snapshots(market_id, engine)
        if len(snapshots) < _MIN_SNAPSHOTS:
            return None

        midpoints = [
            (s.yes_bid + s.yes_ask) / (2.0 * _PRICE_SCALE) for s in snapshots
        ]

        base_p = sum(midpoints) / len(midpoints)

        # Mean-reversion for extreme probabilities.
        if base_p > _HIGH_THRESHOLD or base_p < _LOW_THRESHOLD:
            reversion = _REVERSION_STRENGTH * (0.5 - base_p)
        else:
            reversion = 0.0

        p_model = base_p + reversion

        # Volatility-based confidence.
        vol = _stdev(midpoints)
        historical_calibration = max(0.0, min(1.0, 1.0 - vol / _MAX_VOL))

        conf = ConfidenceScore(
            data_freshness=0.7,
            data_completeness=0.5,
            historical_calibration=historical_calibration,
        )

        reversion_note = (
            f"mean_reversion={reversion:+.3f}" if reversion != 0.0 else "no reversion"
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=conf.overall,
            reasoning=(
                f"Sports model: base_p={base_p:.3f}, {reversion_note}; "
                f"volatility={vol:.3f}, calibration={historical_calibration:.3f}; "
                f"{len(snapshots)} snapshots"
            ),
            data_sources=["price_snapshots"],
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_category(self, market_id: str, engine: Engine) -> Optional[str]:
        """Return the market category as a plain string, or None if not found."""
        from src.models.market import Market

        with get_session(engine) as session:
            row = session.execute(
                select(Market.category).where(Market.market_id == market_id)
            ).first()
            return row[0] if row is not None else None

    def _load_snapshots(self, market_id: str, engine: Engine) -> List[_SnapshotData]:
        """Load price snapshots as plain NamedTuples to avoid DetachedInstanceError."""
        with get_session(engine) as session:
            rows = session.execute(
                select(PriceSnapshot.yes_bid, PriceSnapshot.yes_ask)
                .where(PriceSnapshot.market_id == market_id)
                .order_by(PriceSnapshot.timestamp)
            ).all()
            return [_SnapshotData(yes_bid=r[0], yes_ask=r[1]) for r in rows]
