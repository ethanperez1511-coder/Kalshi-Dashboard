from datetime import datetime, timezone
from src.database import get_session, Base
from src.backtest.models import BacktestRun, BacktestTrade
from src.backtest.report import build_report


def _seed_run(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0, final_bankroll=102.50,
            total_trades=3, wins=2, losses=1,
            total_pnl=2.50, max_drawdown_pct=1.5,
            status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id

        trades = [
            BacktestTrade(
                run_id=run_id, market_id="M1", side="yes",
                entry_price=50, exit_price=100, quantity=2,
                p_model=0.70, implied_prob=0.50, edge=0.20, net_ev=0.19,
                realized_pnl=1.0,
                resolved_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            BacktestTrade(
                run_id=run_id, market_id="M2", side="yes",
                entry_price=60, exit_price=100, quantity=2,
                p_model=0.80, implied_prob=0.60, edge=0.20, net_ev=0.19,
                realized_pnl=0.80,
                resolved_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            ),
            BacktestTrade(
                run_id=run_id, market_id="M3", side="yes",
                entry_price=55, exit_price=0, quantity=3,
                p_model=0.65, implied_prob=0.55, edge=0.10, net_ev=0.09,
                realized_pnl=-1.65,
                resolved_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            ),
        ]
        for t in trades:
            session.add(t)
        session.commit()
    return run_id


def test_report_summary(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert report["run_id"] == run_id
    assert report["total_trades"] == 3
    assert report["wins"] == 2
    assert report["losses"] == 1
    assert abs(report["total_pnl"] - 2.50) < 0.01
    assert abs(report["total_return_pct"] - 2.50) < 0.01


def test_report_win_rate(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert abs(report["win_rate"] - 66.67) < 0.1


def test_report_equity_curve(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    curve = report["equity_curve"]
    assert len(curve) == 4  # initial + 3 trades
    assert curve[0]["bankroll"] == 100.0
    # 100 + 1.0 + 0.80 - 1.65 = 100.15
    assert abs(curve[-1]["bankroll"] - 100.15) < 0.01


def test_report_avg_ev(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    # (0.19 + 0.19 + 0.09) / 3 ≈ 0.1567
    assert abs(report["avg_ev"] - 0.1567) < 0.01


def test_report_calibration_error(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert "calibration_error" in report
    assert report["calibration_error"] >= 0


def test_report_limitations(db_engine):
    run_id = _seed_run(db_engine)
    report = build_report(db_engine, run_id)
    assert "limitations" in report
    assert len(report["limitations"]) >= 3


def test_report_nonexistent_run(db_engine):
    Base.metadata.create_all(db_engine)
    report = build_report(db_engine, 999)
    assert report is None
