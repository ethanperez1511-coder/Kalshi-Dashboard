from __future__ import annotations

from typing import List

from src.modeling.base import BaseModel
from src.modeling.models.consensus import ConsensusModel
from src.modeling.models.finance import FinanceModel
from src.modeling.models.sports import SportsModel


class ModelRegistry:
    """Registry that dispatches models by market category.

    The fallback (ConsensusModel) is always included in results so that
    every market has at least one estimate.
    """

    def __init__(self) -> None:
        self._fallback: BaseModel = ConsensusModel()
        self._specialized: List[BaseModel] = [FinanceModel(), SportsModel()]

    @property
    def all_models(self) -> List[BaseModel]:
        """Return all specialized models followed by the fallback."""
        return self._specialized + [self._fallback]

    def get_models_for(self, category: str) -> List[BaseModel]:
        """Return specialized models matching *category* plus the fallback.

        If no specialized model matches the category, only the fallback is
        returned.
        """
        matching = [m for m in self._specialized if m.category == category]
        return matching + [self._fallback]
