from __future__ import annotations
from typing import Dict, Any, List
from sqlalchemy import Engine
from src.database import get_session
from src.models.position import Position
from src.models.trade import Trade
from src.models.settings import TradingSettings


def total_equity(engine: Engine) -> float:
    """Cash plus the mark-to-market value of open positions.

    This is THE number every risk limit divides by, and the number Kelly sizes
    against. One definition for paper and live, so the 50-trade paper window
    measures the same sizing behaviour live will use.

    `TradingSettings.bankroll` is an equity-at-cost ledger on both paths: it is
    untouched at fill and moves only at settlement, by realized PnL. So it
    already carries the cost basis of everything still open, and marking to
    market is exactly adding unrealized PnL.

    Live's raw Kalshi cash balance is NOT this number — it excludes open
    positions entirely. `sync_live_bankroll` converts before storing.
    """
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        bankroll = settings.bankroll if settings else 0.0
        positions = session.query(Position).filter_by(status="open").all()
        unrealized = sum(p.unrealized_pnl for p in positions)
    return round(bankroll + unrealized, 4)


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
