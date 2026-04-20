from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.ev.calculator import EVResult


@dataclass
class FilterResult:
    """Outcome of evaluating a trade opportunity against the filter criteria."""

    qualifies: bool
    rejection_reasons: List[str] = field(default_factory=list)
    status: str = "rejected"  # "qualifying" | "watching" | "rejected"


class TradeFilter:
    """Filter that decides whether a potential trade is worth executing.

    Parameters
    ----------
    min_daily_volume:
        Minimum daily volume (in contracts) required to trade.
    max_spread_cents:
        Maximum bid-ask spread in cents. Wider spreads indicate illiquidity.
    min_hours_to_expiry:
        Minimum time remaining before market resolution. Very short windows
        increase execution risk.
    """

    def __init__(
        self,
        min_daily_volume: int = 500,
        max_spread_cents: int = 5,
        min_hours_to_expiry: float = 1.0,
    ) -> None:
        self.min_daily_volume = min_daily_volume
        self.max_spread_cents = max_spread_cents
        self.min_hours_to_expiry = min_hours_to_expiry

    def _get_edge_threshold(self, confidence: float) -> float:
        """Return the minimum required edge based on model confidence.

        Higher confidence allows us to act on smaller edges; lower confidence
        demands a larger margin of safety.
        """
        if confidence >= 0.7:
            return 0.05
        if confidence >= 0.4:
            return 0.08
        return 0.12

    def evaluate(
        self,
        ev_result: EVResult,
        confidence: float,
        daily_volume: int,
        bid_ask_spread_cents: int,
        hours_to_expiry: float,
    ) -> FilterResult:
        """Evaluate a potential trade against all filter criteria.

        All five checks must pass for a trade to qualify. If only the volume
        check fails the trade is marked "watching" (might become tradeable with
        more liquidity). Any other failure results in "rejected".

        Parameters
        ----------
        ev_result:
            Output from :func:`~src.ev.calculator.calculate_ev`.
        confidence:
            Model confidence score in [0, 1].
        daily_volume:
            Observed daily trading volume in contracts.
        bid_ask_spread_cents:
            Current bid-ask spread in cents.
        hours_to_expiry:
            Hours remaining until market resolution.
        """
        edge_threshold = self._get_edge_threshold(confidence)
        best_ev = ev_result.best_ev
        best_edge = ev_result.best_edge

        rejection_reasons: List[str] = []
        volume_only_failure = False

        # 1. Best EV must be positive.
        if best_ev <= 0:
            rejection_reasons.append(
                f"EV is non-positive: best_ev={best_ev:.4f}"
            )

        # 2. Edge must meet confidence-adjusted threshold.
        if abs(best_edge) < edge_threshold:
            rejection_reasons.append(
                f"Insufficient edge: |{best_edge:.4f}| < threshold {edge_threshold:.4f}"
            )

        # 3. Volume check.
        volume_passes = daily_volume >= self.min_daily_volume
        if not volume_passes:
            rejection_reasons.append(
                f"Insufficient daily volume: {daily_volume} < {self.min_daily_volume}"
            )

        # 4. Spread check.
        if bid_ask_spread_cents > self.max_spread_cents:
            rejection_reasons.append(
                f"Spread too wide: {bid_ask_spread_cents} > {self.max_spread_cents} cents"
            )

        # 5. Time to expiry check.
        if hours_to_expiry < self.min_hours_to_expiry:
            rejection_reasons.append(
                f"Too close to expiry: {hours_to_expiry:.2f}h < {self.min_hours_to_expiry:.2f}h"
            )

        qualifies = len(rejection_reasons) == 0

        if qualifies:
            status = "qualifying"
        elif not volume_passes and len(rejection_reasons) == 1:
            # Only the volume check failed — worth watching.
            status = "watching"
        else:
            status = "rejected"

        return FilterResult(
            qualifies=qualifies,
            rejection_reasons=rejection_reasons,
            status=status,
        )
