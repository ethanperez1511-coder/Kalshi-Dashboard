from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy import Engine, select

from src.database import get_session
from src.modeling.base import BaseModel, ModelResult
from src.modeling.confidence import ConfidenceScore
from src.models.price import PriceSnapshot
from src.trading_config import ENABLE_KEYWORD_PRIORS

# Kalshi prices are integers in [0, 100] representing cents per dollar.
_PRICE_SCALE = 100.0

# Minimum number of snapshots required to produce an estimate.
_MIN_SNAPSHOTS = 5

# Weight given to the keyword-derived prior when blending with market consensus.
_PRIOR_WEIGHT = 0.30
_MARKET_WEIGHT = 1.0 - _PRIOR_WEIGHT

# Keyword → prior probability mapping.
# Keys are lowercase; matching is done on the lowercased market title.
_KEYWORD_PRIORS: Dict[str, float] = {
    "fed": 0.50,
    "federal reserve": 0.50,
    "rate": 0.45,
    "cpi": 0.55,
    "inflation": 0.55,
    "gdp": 0.50,
    "recession": 0.20,
    "s&p": 0.55,
    "bitcoin": 0.50,
    "btc": 0.50,
    "unemployment": 0.45,
    "jobs": 0.50,
    "payroll": 0.50,
    "yield": 0.50,
    "treasury": 0.50,
    "dollar": 0.50,
    "oil": 0.50,
    "gold": 0.55,
}


class _SnapshotData(NamedTuple):
    yes_bid: int
    yes_ask: int


def _find_keyword_prior(title: str) -> Optional[Tuple[str, float]]:
    """Return the first matching (keyword, prior) pair found in *title*, or None."""
    lower = title.lower()
    for keyword, prior in _KEYWORD_PRIORS.items():
        if keyword in lower:
            return keyword, prior
    return None


class FinanceModel(BaseModel):
    """Economics-category model that blends keyword priors with market consensus.

    Algorithm
    ---------
    1. Return None if the market category is not "Economics".
    2. Return None if there are fewer than 5 price snapshots.
    3. Compute market consensus: average midpoint of all snapshots.
    4. Search the title for recognised finance keywords.
       - If found: p_model = 0.30 * prior + 0.70 * consensus
       - If not found: p_model = consensus (keyword prior not applicable)
    5. Build a ConfidenceScore:
       - data_freshness          : 0.7 (fixed)
       - data_completeness       : 0.6 if keyword matched, 0.4 otherwise
       - historical_calibration  : 0.5 (fixed)
    """

    @property
    def category(self) -> str:
        return "Economics"

    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        # Gate: wrong category (the caller supplies the market title; category is
        # checked via the market_id / category stored in the DB, but the spec says
        # "Returns None if market category != 'Economics'".  The category is passed
        # implicitly via the routing layer — here we accept it as a parameter-free
        # check by fetching it from the DB.)
        category = self._load_category(market_id, engine)
        if category is None or category != "Economics":
            return None

        snapshots = self._load_snapshots(market_id, engine)
        if len(snapshots) < _MIN_SNAPSHOTS:
            return None

        midpoints = [
            (s.yes_bid + s.yes_ask) / (2.0 * _PRICE_SCALE) for s in snapshots
        ]
        consensus = sum(midpoints) / len(midpoints)

        keyword_match = _find_keyword_prior(title) if ENABLE_KEYWORD_PRIORS else None

        if keyword_match is not None:
            keyword, prior = keyword_match
            p_model = _PRIOR_WEIGHT * prior + _MARKET_WEIGHT * consensus
            data_completeness = 0.6
            keyword_note = f"keyword='{keyword}' prior={prior:.2f}"
        else:
            p_model = consensus
            data_completeness = 0.4
            keyword_note = "no keyword match"

        conf = ConfidenceScore(
            data_freshness=0.7,
            data_completeness=data_completeness,
            historical_calibration=0.5,
        )

        return ModelResult(
            market_id=market_id,
            p_model=p_model,
            confidence=conf.overall,
            reasoning=(
                f"Finance model: consensus={consensus:.3f}, {keyword_note}; "
                f"p_model={p_model:.3f} from {len(snapshots)} snapshots"
            ),
            data_sources=["price_snapshots", "keyword_priors"],
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
