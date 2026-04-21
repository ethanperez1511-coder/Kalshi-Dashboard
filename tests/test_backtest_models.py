from datetime import datetime, timezone
from src.database import get_session, Base
from src.backtest.models import BacktestRun, BacktestTrade


def test_create_backtest_run(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0,
            final_bankroll=112.50,
            total_trades=20,
            wins=14,
            losses=6,
            total_pnl=12.50,
            max_drawdown_pct=4.2,
            status="completed",
        )
        session.add(run)
        session.commit()
        fetched = session.query(BacktestRun).first()
        assert fetched.total_trades == 20
        assert fetched.status == "completed"
        assert fetched.total_pnl == 12.50


def test_create_backtest_trade(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0, final_bankroll=100.0,
            total_trades=0, wins=0, losses=0,
            total_pnl=0, max_drawdown_pct=0,
            status="completed",
        )
        session.add(run)
        session.flush()

        trade = BacktestTrade(
            run_id=run.id,
            market_id="DEMO-FED-RATE-JUL",
            side="yes",
            entry_price=65,
            exit_price=100,
            quantity=2,
            p_model=0.77,
            implied_prob=0.65,
            edge=0.12,
            net_ev=0.10,
            realized_pnl=0.70,
            resolved_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        session.add(trade)
        session.commit()
        fetched = session.query(BacktestTrade).first()
        assert fetched.market_id == "DEMO-FED-RATE-JUL"
        assert fetched.realized_pnl == 0.70
        assert fetched.run_id == run.id


def test_backtest_run_has_trades(db_engine):
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        run = BacktestRun(
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            initial_bankroll=100.0, final_bankroll=101.0,
            total_trades=1, wins=1, losses=0,
            total_pnl=1.0, max_drawdown_pct=0,
            status="completed",
        )
        session.add(run)
        session.flush()
        session.add(BacktestTrade(
            run_id=run.id, market_id="MKT-1", side="yes",
            entry_price=50, exit_price=100, quantity=2,
            p_model=0.7, implied_prob=0.5, edge=0.2, net_ev=0.19,
            realized_pnl=1.0,
            resolved_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ))
        session.commit()

        fetched_run = session.query(BacktestRun).first()
        trades = session.query(BacktestTrade).filter_by(run_id=fetched_run.id).all()
        assert len(trades) == 1
