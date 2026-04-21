from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from sqlalchemy import Engine
from src.database import get_session
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings

logger = logging.getLogger(__name__)


class PortfolioTracker:
    def __init__(self, engine: Engine):
        self._engine = engine

    def close_position(
        self, market_id: str, exit_price: int,
    ) -> Optional[Dict[str, Any]]:
        with get_session(self._engine) as session:
            pos = (
                session.query(Position)
                .filter_by(market_id=market_id, status="open")
                .first()
            )
            if not pos:
                return None

            # Calculate realized PnL
            if pos.side == "yes":
                realized_pnl = (exit_price - pos.entry_price) * pos.quantity / 100.0
            else:
                realized_pnl = (pos.entry_price - exit_price) * pos.quantity / 100.0

            # Close the position
            pos.status = "closed"
            pos.current_price = exit_price
            pos.closed_at = datetime.now(timezone.utc)

            # Update the trade record
            trade = (
                session.query(Trade)
                .filter_by(market_id=market_id, status="filled")
                .order_by(Trade.created_at.desc())
                .first()
            )
            if trade:
                trade.status = "closed"
                trade.exit_price = exit_price
                trade.realized_pnl = realized_pnl

            # Update bankroll
            settings = session.query(TradingSettings).first()
            if settings:
                settings.bankroll = round(settings.bankroll + realized_pnl, 2)
                if settings.bankroll > settings.peak_bankroll:
                    settings.peak_bankroll = settings.bankroll

            session.commit()

            logger.info(
                f"Closed {market_id} @ {exit_price}c — PnL ${realized_pnl:.2f}"
            )

            return {
                "market_id": market_id,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "status": "closed",
            }

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with get_session(self._engine) as session:
            positions = session.query(Position).filter_by(status="open").all()
            return [
                {
                    "market_id": p.market_id,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "quantity": p.quantity,
                    "unrealized_pnl": p.unrealized_pnl,
                    "cost_basis": p.cost_basis,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                }
                for p in positions
            ]

    def get_summary(self) -> Dict[str, Any]:
        with get_session(self._engine) as session:
            settings = session.query(TradingSettings).first()
            if not settings:
                return {
                    "bankroll": 0, "open_position_count": 0,
                    "total_exposure": 0, "total_return_pct": 0,
                    "max_drawdown_pct": 0, "unrealized_pnl": 0,
                }

            positions = session.query(Position).filter_by(status="open").all()
            total_exposure = sum(p.cost_basis for p in positions)
            unrealized_pnl = sum(p.unrealized_pnl for p in positions)

            initial_bankroll = 100.0
            total_return_pct = (
                (settings.bankroll - initial_bankroll) / initial_bankroll * 100
                if initial_bankroll > 0 else 0
            )

            max_drawdown_pct = 0.0
            if settings.peak_bankroll > 0:
                max_drawdown_pct = (
                    (settings.peak_bankroll - settings.bankroll)
                    / settings.peak_bankroll * 100
                )

            return {
                "bankroll": settings.bankroll,
                "peak_bankroll": settings.peak_bankroll,
                "open_position_count": len(positions),
                "total_exposure": round(total_exposure, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_return_pct": round(total_return_pct, 2),
                "max_drawdown_pct": round(max_drawdown_pct, 2),
            }
