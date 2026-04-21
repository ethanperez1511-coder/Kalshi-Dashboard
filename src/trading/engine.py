from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.position import Position
from src.models.settings import TradingSettings
from src.risk.manager import TradeDecision

logger = logging.getLogger(__name__)


class TradeEngine:
    def __init__(self, engine: Engine):
        self._engine = engine

    def _get_mode(self) -> dict:
        with get_session(self._engine) as session:
            s = session.query(TradingSettings).first()
            if not s:
                return {"mode": "paper", "paper_trade_count": 0, "paper_trades_before_live": 50}
            return {
                "mode": s.mode,
                "paper_trade_count": s.paper_trade_count,
                "paper_trades_before_live": s.paper_trades_before_live,
            }

    def can_trade_live(self) -> bool:
        info = self._get_mode()
        if info["mode"] != "live":
            return False
        if info["paper_trade_count"] < info["paper_trades_before_live"]:
            return False
        return True

    def execute(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
    ) -> Optional[Dict[str, Any]]:
        if not decision.approved:
            logger.info(f"Trade rejected for {market_id}: {decision.rejection_reasons}")
            return None

        mode_info = self._get_mode()
        is_paper = mode_info["mode"] == "paper" or not self.can_trade_live()

        if is_paper:
            return self._execute_paper(
                decision, market_id, p_model, implied_prob,
                edge, net_ev, confidence, reasoning,
            )
        else:
            # Live execution placeholder
            raise NotImplementedError("Live trading requires Kalshi API credentials")

    def _execute_paper(
        self,
        decision: TradeDecision,
        market_id: str,
        p_model: float,
        implied_prob: float,
        edge: float,
        net_ev: float,
        confidence: float,
        reasoning: str,
    ) -> Dict[str, Any]:
        with get_session(self._engine) as session:
            # Create trade record
            trade = Trade(
                market_id=market_id,
                side=decision.side,
                action="buy",
                price=decision.price_cents,
                quantity=decision.quantity,
                p_model=p_model,
                implied_prob=implied_prob,
                edge=edge,
                net_ev=net_ev,
                position_size_dollars=decision.position_size_dollars,
                confidence=confidence,
                reasoning=reasoning,
                is_paper=True,
                status="filled",
            )
            session.add(trade)

            # Create or update position
            existing_pos = (
                session.query(Position)
                .filter_by(market_id=market_id, side=decision.side, status="open")
                .first()
            )
            if existing_pos:
                existing_pos.quantity += decision.quantity
            else:
                pos = Position(
                    market_id=market_id,
                    side=decision.side,
                    entry_price=decision.price_cents,
                    quantity=decision.quantity,
                    current_price=decision.price_cents,
                    status="open",
                )
                session.add(pos)

            # Increment paper trade count
            settings = session.query(TradingSettings).first()
            if settings:
                settings.paper_trade_count += 1

            session.commit()

            logger.info(
                f"Paper trade executed: {market_id} {decision.side} "
                f"x{decision.quantity} @ {decision.price_cents}c "
                f"(${decision.position_size_dollars:.2f})"
            )

            return {
                "market_id": market_id,
                "side": decision.side,
                "price": decision.price_cents,
                "quantity": decision.quantity,
                "dollars": decision.position_size_dollars,
                "is_paper": True,
                "status": "filled",
            }
