from __future__ import annotations
from typing import Dict, Any
from sqlalchemy import Engine
from src.database import get_session
from src.models.trade import Trade
from src.models.settings import TradingSettings


def compute_metrics(engine: Engine) -> Dict[str, Any]:
    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        initial_bankroll = 100.0

        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.created_at)
            .all()
        )

        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "total_return_pct": 0,
                "avg_edge": 0, "avg_ev": 0, "calibration_error": 0,
                "avg_pnl_per_trade": 0,
            }

        wins = [t for t in trades if (t.realized_pnl or 0) > 0]
        losses = [t for t in trades if (t.realized_pnl or 0) <= 0]
        total_pnl = sum(t.realized_pnl or 0 for t in trades)
        avg_edge = sum(t.edge for t in trades) / len(trades)
        avg_ev = sum(t.net_ev for t in trades) / len(trades)

        win_rate = len(wins) / len(trades) * 100
        total_return_pct = total_pnl / initial_bankroll * 100

        # Calibration: avg predicted probability vs actual win frequency
        avg_p_model = sum(t.p_model for t in trades) / len(trades)
        actual_win_rate = len(wins) / len(trades)
        calibration_error = abs(avg_p_model - actual_win_rate)

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "avg_edge": round(avg_edge, 4),
            "avg_ev": round(avg_ev, 4),
            "calibration_error": round(calibration_error, 4),
            "avg_pnl_per_trade": round(total_pnl / len(trades), 4),
        }
