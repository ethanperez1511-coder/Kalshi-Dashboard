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

    def __post_init__(self) -> None:
        self.p_model = max(0.0, min(1.0, self.p_model))
        self.confidence = max(0.0, min(1.0, self.confidence))


# Model type tags for edge quality separation
MODEL_TYPE_INDEPENDENT = "INDEPENDENT"      # Has external data (real edge)
MODEL_TYPE_PRICE_DERIVED = "PRICE_DERIVED"  # Anchored to market price


class BaseModel(ABC):
    @property
    @abstractmethod
    def category(self) -> str:
        ...

    @property
    def model_type(self) -> str:
        """Override in subclass. Default is PRICE_DERIVED."""
        return MODEL_TYPE_PRICE_DERIVED

    def matches(self, category: str) -> bool:
        """Whether this model covers *category*. Default: exact match.

        Models spanning several categories override this.
        """
        return category == self.category

    @abstractmethod
    def estimate(
        self,
        market_id: str,
        title: str,
        current_price: float,
        engine: Engine,
    ) -> Optional[ModelResult]:
        ...
