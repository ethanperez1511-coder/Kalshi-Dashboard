from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import Engine
from src.database import get_session
from src.ev.calculator import EVResult
from src.risk.kelly import kelly_size, calibration_shrinkage
from src.risk.limits import LimitsChecker
from src.portfolio.equity import total_equity
from src.models.settings import TradingSettings
from src.models.trade import Trade
from src.trading_config import MIN_SETTLED_TRADES

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

        # Compute shrinkage multiplier from calibration (if enough data)
        shrinkage = 1.0
        with get_session(self._engine) as session:
            # Legacy trades were placed by superseded code, so they say nothing
            # about how the current system is calibrated.
            closed_q = session.query(Trade).filter(
                Trade.status == "closed", Trade.is_legacy.is_(False)
            )
            closed_count = closed_q.count()
            if closed_count >= MIN_SETTLED_TRADES:
                closed_trades = closed_q.all()
                wins = [t for t in closed_trades if (t.realized_pnl or 0) > 0]
                avg_p = sum(t.p_model for t in closed_trades) / len(closed_trades)
                actual_wr = len(wins) / len(closed_trades)
                cal_err = abs(avg_p - actual_wr)
                shrinkage = calibration_shrinkage(cal_err)

        # Kelly sizes against total equity (cash + mark-to-market open
        # positions) — the same number the limits divide by, never the raw
        # ledger field, which used to mean different things on the two paths.
        equity = total_equity(self._engine)
        p = ev_result.p_model if side == "yes" else (1 - ev_result.p_model)
        kelly = kelly_size(
            p_model=p,
            price_cents=price_cents,
            bankroll=equity,
            kelly_fraction=settings.kelly_fraction,
            shrinkage_multiplier=shrinkage,
        )

        # Cap at max single trade
        max_trade = equity * settings.max_single_trade_pct
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
