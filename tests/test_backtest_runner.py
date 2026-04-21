import random
from datetime import datetime, timedelta, timezone
from src.database import get_session, Base
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.backtest.models import BacktestRun, BacktestTrade
from src.backtest.runner import BacktestRunner


def _seed_historical_markets(db_engine):
    """Create markets with price history and known close dates (in the past)."""
    Base.metadata.create_all(db_engine)
    with get_session(db_engine) as session:
        # Market 1: Sports, close in March (price base ~45)
        session.add(Market(
            market_id="BT-MKT-1", title="Will Lakers win game 5?",
            category="Sports", close_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            status="closed",
        ))
        random.seed(42)
        base = 45
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            spread = 2
            session.add(PriceSnapshot(
                market_id="BT-MKT-1", yes_bid=price, yes_ask=price + spread,
                last_price=price + 1, volume=2000, timestamp=ts,
            ))
            base = price

        # Market 2: Economics, close in March (price base ~70)
        session.add(Market(
            market_id="BT-MKT-2", title="Will Fed raise rates in March?",
            category="Economics", close_date=datetime(2026, 3, 20, tzinfo=timezone.utc),
            status="closed",
        ))
        base = 70
        for i in range(24):
            ts = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=i * 6)
            drift = random.randint(-2, 2)
            price = max(5, min(95, base + drift))
            spread = 2
            session.add(PriceSnapshot(
                market_id="BT-MKT-2", yes_bid=price, yes_ask=price + spread,
                last_price=price + 1, volume=3000, timestamp=ts,
            ))
            base = price

        session.commit()


def test_backtest_runner_creates_run(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    assert run_id is not None

    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        assert run.status == "completed"
        assert run.initial_bankroll == 100.0


def test_backtest_runner_produces_trades(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        trades = session.query(BacktestTrade).filter_by(run_id=run_id).all()
        run = session.query(BacktestRun).get(run_id)
        assert run.total_trades == len(trades)


def test_backtest_runner_updates_bankroll(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        assert abs(run.final_bankroll - (100.0 + run.total_pnl)) < 0.01


def test_backtest_runner_category_filter(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 3, 31, tzinfo=timezone.utc),
        initial_bankroll=100.0,
        category_filter="Sports",
    )
    with get_session(db_engine) as session:
        trades = session.query(BacktestTrade).filter_by(run_id=run_id).all()
        for t in trades:
            assert t.market_id == "BT-MKT-1"


def test_backtest_runner_empty_range(db_engine):
    _seed_historical_markets(db_engine)
    runner = BacktestRunner(db_engine)
    run_id = runner.run(
        start_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2025, 2, 1, tzinfo=timezone.utc),
        initial_bankroll=100.0,
    )
    with get_session(db_engine) as session:
        run = session.query(BacktestRun).get(run_id)
        assert run.total_trades == 0
        assert run.final_bankroll == 100.0
