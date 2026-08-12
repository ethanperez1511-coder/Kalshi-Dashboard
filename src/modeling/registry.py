from __future__ import annotations

from typing import List

from src.modeling.base import BaseModel
from src.modeling.models.consensus import ConsensusModel
from src.modeling.models.finance import FinanceModel
from src.modeling.models.polymarket import PolymarketModel
from src.modeling.models.sports import SportsModel
from src.modeling.models.sports_odds import SportsOddsModel
from src.weather.model import WeatherModel
from src.modeling.odds_api import OddsClient


class ModelRegistry:
    """Registry that dispatches models by market category.

    The fallback (ConsensusModel) is always included in results so that
    every market has at least one estimate.
    """

    def __init__(self, odds_client: OddsClient | None = None) -> None:
        self._fallback: BaseModel = ConsensusModel()
        self._specialized: List[BaseModel] = [
            SportsOddsModel(odds_client),  # external odds first
            WeatherModel(),                # calibrated NWS MOS guidance
            PolymarketModel(),
            FinanceModel(),
            SportsModel(),
        ]

    @property
    def all_models(self) -> List[BaseModel]:
        """Return all specialized models followed by the fallback."""
        return self._specialized + [self._fallback]

    def get_models_for(
        self, category: str, market_id: str = "",
    ) -> List[BaseModel]:
        """Return specialized models claiming this market plus the fallback.

        Dispatch asks each model whether it claims the market, not merely
        whether it claims the category. `claims()` defaults to the category
        test, so this is identical for every model whose scope is a category.
        """
        matching = [m for m in self._specialized if m.claims(market_id, category)]
        return matching + [self._fallback]
