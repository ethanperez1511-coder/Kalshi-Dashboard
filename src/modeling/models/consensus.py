from __future__ import annotations

from typing import List, NamedTuple, Optional

from sqlalchemy import Engine, select

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.price import PriceSnapshot

# Kalshi prices are integers in the range [0, 100] representing cents per dollar.
_PRICE_SCALE = 100.0

# Minimum number of snapshots required to produce an estimate.
_MIN_SNAPSHOTS = 5

# Maximum spread considered "tight" (full spread width in price ticks).
_TIGHT_SPREAD = 2.0
# A spread at or above this is considered "wide" and drives completeness to 0.
_WIDE_SPREAD = 10.0


class _SnapshotData(NamedTuple):
    yes_bid: int
    yes_ask: int


class ConsensusModel(BaseModel):
    """Fallback model that estimates probability from price history alone.

    Algorithm
    ---------
    1. Compute the midpoint of each snapshot: (yes_bid + yes_ask) / 2.
    2. Average all midpoints → base_p (in [0, 1]).
    3. Detect a linear trend over the midpoints; if the slope is positive,
       nudge base_p upward proportionally.
    4. Build a ConfidenceScore:
       - data_freshness   : capped at 1.0 for ≥ 50 snapshots, linear below.
       - data_completeness: based on average spread width (narrow = high).
       - historical_calibration: fixed at 0.4 (no external calibration data).
    """

    @property
    def category(self) -> str:
        return "fallback"

    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        snapshots = self._load_snapshots(market_id, engine)

        if len(snapshots) < _MIN_SNAPSHOTS:
            return None

        midpoints = [
            (s.yes_bid + s.yes_ask) / (2.0 * _PRICE_SCALE) for s in snapshots
        ]
        spreads = [(s.yes_ask - s.yes_bid) for s in snapshots]

        base_p = sum(midpoints) / len(midpoints)

        # Trend: simple linear slope over midpoint indices.
        trend_adjustment = self._compute_trend_adjustment(midpoints)
        p_model = base_p + trend_adjustment

        # Confidence components
        data_freshness = min(1.0, len(snapshots) / 50.0)

        avg_spread = sum(spreads) / len(spreads)
        data_completeness = max(
            0.0,
            min(1.0, 1.0 - (avg_spread - _TIGHT_SPREAD) / (_WIDE_SPREAD - _TIGHT_SPREAD)),
        )

        conf = ConfidenceScore(
            data_freshness=data_freshness,
            data_completeness=data_completeness,
            historical_calibration=0.4,
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=conf.overall,
            reasoning=(
                f"Consensus from {len(snapshots)} price snapshots; "
                f"midpoint avg={base_p:.3f}, trend_adj={trend_adjustment:.3f}"
            ),
            data_sources=["price_snapshots"],
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_snapshots(self, market_id: str, engine: Engine) -> List[_SnapshotData]:
        """Load price snapshots as plain data objects to avoid DetachedInstanceError."""
        with get_session(engine) as session:
            rows = session.execute(
                select(PriceSnapshot.yes_bid, PriceSnapshot.yes_ask)
                .where(PriceSnapshot.market_id == market_id)
                .order_by(PriceSnapshot.timestamp)
            ).all()
            # rows are plain Row tuples — safe to use after session closes.
            return [_SnapshotData(yes_bid=r[0], yes_ask=r[1]) for r in rows]

    @staticmethod
    def _compute_trend_adjustment(midpoints: List[float]) -> float:
        """Return a small additive adjustment based on the linear trend.

        Uses least-squares slope normalised by the number of points so that
        the adjustment is bounded and shrinks for short series.
        """
        n = len(midpoints)
        if n < 2:
            return 0.0

        xs = list(range(n))
        mean_x = (n - 1) / 2.0
        mean_y = sum(midpoints) / n

        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, midpoints))
        denominator = sum((x - mean_x) ** 2 for x in xs)

        if denominator == 0:
            return 0.0

        slope = numerator / denominator  # units: probability per snapshot step

        # Scale: total rise over the series as a fraction of [0,1], capped at ±0.10
        total_rise = slope * (n - 1)
        return max(-0.10, min(0.10, total_rise * 0.5))
