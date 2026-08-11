from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.ev.calculator import EVResult
from src.trading_config import (
    MAX_MODEL_DISAGREEMENT,
    SKIP_YES_LONGSHOTS,
    YES_LONGSHOT_MAX_PRICE_CENTS,
)


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
    max_disagreement:
        Maximum allowed |p_model - market price| on the recommended side.
        Larger disagreement signals bad input data rather than real edge.
    skip_yes_longshots:
        Reject YES buys priced below ``yes_longshot_max_price_cents``.
        Fading longshots (NO side) is unaffected.
    """

    def __init__(
        self,
        min_daily_volume: int = 100,
        max_spread_cents: int = 15,
        min_hours_to_expiry: float = 1.0,
        max_disagreement: float = MAX_MODEL_DISAGREEMENT,
        skip_yes_longshots: bool = SKIP_YES_LONGSHOTS,
        yes_longshot_max_price_cents: int = YES_LONGSHOT_MAX_PRICE_CENTS,
        max_hours_to_expiry: float = 0.0,
    ) -> None:
        self.min_daily_volume = min_daily_volume
        self.max_spread_cents = max_spread_cents
        self.min_hours_to_expiry = min_hours_to_expiry
        self.max_disagreement = max_disagreement
        self.skip_yes_longshots = skip_yes_longshots
        self.yes_longshot_max_price_cents = yes_longshot_max_price_cents
        # 0 disables the far-expiry cap.
        self.max_hours_to_expiry = max_hours_to_expiry

    def _get_edge_threshold(self, confidence: float) -> float:
        """Return the minimum required edge based on model confidence.

        Higher confidence allows us to act on smaller edges; lower confidence
        demands a larger margin of safety.
        """
        if confidence >= 0.7:
            return 0.03
        if confidence >= 0.4:
            return 0.05
        return 0.08

    def prescreen(
        self,
        daily_volume: int,
        bid_ask_spread_cents: int,
        hours_to_expiry: float,
    ) -> bool:
        """Can this market qualify at all, before any model has run?

        Only market facts are consulted — volume, spread, expiry — never
        anything derived from a probability estimate. Every check here is a
        verbatim copy of the corresponding check in :meth:`evaluate`, so a
        market rejected here would have been rejected there too: skipping the
        model is an optimisation, not a decision. `tests/test_odds_quota.py`
        asserts that subset property over a grid.

        The point is cost: model dispatch can spend a metered odds request, and
        spending it on a market that fails a liquidity gate is pure waste.
        """
        if daily_volume < self.min_daily_volume:
            return False
        if bid_ask_spread_cents > self.max_spread_cents:
            return False
        if hours_to_expiry < self.min_hours_to_expiry:
            return False
        if self.max_hours_to_expiry > 0 and hours_to_expiry > self.max_hours_to_expiry:
            return False
        return True

    def evaluate(
        self,
        ev_result: EVResult,
        confidence: float,
        daily_volume: int,
        bid_ask_spread_cents: int,
        hours_to_expiry: float,
        min_edge_override: float = None,
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
        if min_edge_override is not None:
            edge_threshold = max(edge_threshold, min_edge_override)
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

        # 5b. Far-expiry cap: capital locked in a months-out market can't be
        # recycled. 0 disables.
        if self.max_hours_to_expiry > 0 and hours_to_expiry > self.max_hours_to_expiry:
            rejection_reasons.append(
                f"Expiry too far out: {hours_to_expiry:.0f}h > {self.max_hours_to_expiry:.0f}h"
            )

        # 6. Disagreement cap: |p_model - market| beyond this means bad data, not edge.
        if abs(best_edge) > self.max_disagreement + 1e-9:  # epsilon: boundary stays tradeable
            rejection_reasons.append(
                f"Model-market disagreement too large: |{best_edge:.4f}| > {self.max_disagreement:.4f}"
            )

        # 7. YES longshot skip: buying cheap YES contracts has shown persistent losses.
        price_cents = ev_result.implied_prob * 100.0
        if (
            self.skip_yes_longshots
            and ev_result.recommended_side == "yes"
            and price_cents < self.yes_longshot_max_price_cents
        ):
            rejection_reasons.append(
                f"YES longshot blocked: price {price_cents:.0f}c < {self.yes_longshot_max_price_cents}c"
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
