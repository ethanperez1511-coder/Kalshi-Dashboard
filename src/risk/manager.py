from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import Engine
from src.database import get_session
from src.ev.calculator import EVResult
from src.risk.kelly import kelly_size
from src.risk.limits import LimitsChecker
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    approved: bool
    side: str
    position_size_dollars: float
    quantity: int
    price_cents: int
    rejection_reasons: list


class RiskManager:
    def __init__(self, engine: Engine):
        self._engine = engine
        self._limits = LimitsChecker(engine)

    def evaluate(
        self,
        ev_result: EVResult,
        confidence: float,
        market_id: str,
        market_category: str,
    ) -> TradeDecision:
        # Get settings
        settings = TradingSettings.get_or_create(self._engine)
        side = ev_result.recommended_side
        price_cents = int(ev_result.implied_prob * 100)

        if side == "no":
            price_cents = 100 - price_cents

        # Kelly sizing
        p = ev_result.p_model if side == "yes" else (1 - ev_result.p_model)
        kelly = kelly_size(
            p_model=p,
            price_cents=price_cents,
            bankroll=settings.bankroll,
            kelly_fraction=settings.kelly_fraction,
        )

        # Cap at max single trade
        max_trade = settings.bankroll * settings.max_single_trade_pct
        dollars = min(kelly.recommended_dollars, max_trade)
        contract_cost = price_cents / 100.0
        quantity = int(dollars / contract_cost) if contract_cost > 0 else 0

        if quantity == 0 or dollars <= 0:
            return TradeDecision(
                approved=False, side=side,
                position_size_dollars=0, quantity=0,
                price_cents=price_cents,
                rejection_reasons=["Position size too small"],
            )

        # Check hard limits
        limits_result = self._limits.check(dollars, market_id, market_category)

        if not limits_result.approved:
            return TradeDecision(
                approved=False, side=side,
                position_size_dollars=dollars, quantity=quantity,
                price_cents=price_cents,
                rejection_reasons=limits_result.violations,
            )

        return TradeDecision(
            approved=True, side=side,
            position_size_dollars=round(dollars, 2),
            quantity=quantity,
            price_cents=price_cents,
            rejection_reasons=[],
        )
