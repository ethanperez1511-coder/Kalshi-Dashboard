from __future__ import annotations
from typing import Dict, Any, List
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.settings import TradingSettings


def build_equity_curve(
    engine: Engine, initial_bankroll: float = 100.0,
) -> List[Dict[str, Any]]:
    with get_session(engine) as session:
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.created_at)
            .all()
        )

        curve = [{"timestamp": None, "bankroll": initial_bankroll, "peak": initial_bankroll}]

        bankroll = initial_bankroll
        peak = initial_bankroll

        for trade in trades:
            bankroll = round(bankroll + (trade.realized_pnl or 0), 2)
            peak = max(peak, bankroll)
            curve.append({
                "timestamp": trade.created_at.isoformat() if trade.created_at else None,
                "bankroll": bankroll,
                "peak": peak,
                "market_id": trade.market_id,
                "pnl": trade.realized_pnl or 0,
            })

        return curve
