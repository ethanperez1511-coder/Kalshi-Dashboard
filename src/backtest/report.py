from __future__ import annotations
from typing import Dict, Any, Optional
from sqlalchemy import Engine
from src.database import get_session
from src.backtest.models import BacktestRun, BacktestTrade

LIMITATIONS = [
    "Historical data availability may be limited",
    "Backtests assume fills at historical prices (no slippage)",
    "Past performance does not guarantee future results",
]


def build_report(engine: Engine, run_id: int) -> Optional[Dict[str, Any]]:
    with get_session(engine) as session:
        run = session.query(BacktestRun).get(run_id)
        if not run:
            return None

        trades = (
            session.query(BacktestTrade)
            .filter_by(run_id=run_id)
            .order_by(BacktestTrade.resolved_at)
            .all()
        )

        # Equity curve
        bankroll = run.initial_bankroll
        peak = bankroll
        curve = [{"timestamp": None, "bankroll": bankroll, "peak": peak}]
        for t in trades:
            bankroll = round(bankroll + t.realized_pnl, 2)
            peak = max(peak, bankroll)
            curve.append({
                "timestamp": t.resolved_at.isoformat() if t.resolved_at else None,
                "bankroll": bankroll,
                "peak": peak,
                "market_id": t.market_id,
                "pnl": t.realized_pnl,
            })

        # Metrics
        win_rate = (run.wins / run.total_trades * 100) if run.total_trades > 0 else 0
        total_return_pct = (
            run.total_pnl / run.initial_bankroll * 100
            if run.initial_bankroll > 0 else 0
        )

        avg_ev = 0.0
        avg_edge = 0.0
        calibration_error = 0.0
        if trades:
            avg_ev = sum(t.net_ev for t in trades) / len(trades)
            avg_edge = sum(t.edge for t in trades) / len(trades)
            avg_p_model = sum(t.p_model for t in trades) / len(trades)
            actual_win_rate = run.wins / run.total_trades if run.total_trades > 0 else 0
            calibration_error = abs(avg_p_model - actual_win_rate)

        return {
            "run_id": run_id,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "initial_bankroll": run.initial_bankroll,
            "final_bankroll": run.final_bankroll,
            "total_trades": run.total_trades,
            "wins": run.wins,
            "losses": run.losses,
            "total_pnl": run.total_pnl,
            "total_return_pct": round(total_return_pct, 2),
            "win_rate": round(win_rate, 2),
            "max_drawdown_pct": run.max_drawdown_pct,
            "avg_ev": round(avg_ev, 4),
            "avg_edge": round(avg_edge, 4),
            "calibration_error": round(calibration_error, 4),
            "equity_curve": curve,
            "limitations": LIMITATIONS,
        }
