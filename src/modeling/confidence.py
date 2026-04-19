from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConfidenceScore:
    data_freshness: float
    data_completeness: float
    historical_calibration: float

    @property
    def overall(self) -> float:
        raw = (
            0.3 * self.data_freshness
            + 0.3 * self.data_completeness
            + 0.4 * self.historical_calibration
        )
        return max(0.0, min(1.0, raw))

    @property
    def tier(self) -> str:
        if self.overall >= 0.7:
            return "high"
        elif self.overall >= 0.4:
            return "medium"
        else:
            return "low"

    @property
    def edge_threshold(self) -> float:
        if self.tier == "high":
            return 0.05
        elif self.tier == "medium":
            return 0.08
        else:
            return 0.12
