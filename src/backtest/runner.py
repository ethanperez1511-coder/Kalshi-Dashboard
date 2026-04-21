from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Engine, select
from src.database import get_session
from src.ev.calculator import calculate_ev
from src.modeling.registry import ModelRegistry
from src.models.market import Market
from src.models.price import PriceSnapshot
from src.backtest.models import BacktestRun, BacktestTrade

logger = logging.getLogger(__name__)

# Backtest uses its own simplified risk params instead of TradingSettings
_BT_KELLY_FRACTION = 0.25
_BT_MAX_SINGLE_TRADE_PCT = 0.03
_BT_MIN_EDGE = 0.05


class BacktestRunner:
    def __init__(self, engine: Engine):
        self._engine = engine

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        initial_bankroll: float = 100.0,
        category_filter: Optional[str] = None,
    ) -> int:
        # Create backtest run record
        with get_session(self._engine) as session:
            bt_run = BacktestRun(
                start_date=start_date,
                end_date=end_date,
                initial_bankroll=initial_bankroll,
                final_bankroll=initial_bankroll,
                status="running",
                category_filter=category_filter,
            )
            session.add(bt_run)
            session.commit()
            run_id = bt_run.id

        # Load markets with close dates in range
        with get_session(self._engine) as session:
            query = (
                select(
                    Market.market_id,
                    Market.title,
                    Market.category,
                    Market.close_date,
                )
                .where(Market.close_date >= start_date)
                .where(Market.close_date <= end_date)
            )
            if category_filter:
                query = query.where(Market.category == category_filter)
            rows = session.execute(query).all()
            markets = [
                {"market_id": r[0], "title": r[1], "category": r[2], "close_date": r[3]}
                for r in rows
            ]

        bankroll = initial_bankroll
        peak = initial_bankroll
        max_drawdown_pct = 0.0
        registry = ModelRegistry()
        trades = []

        for mkt in markets:
            market_id = mkt["market_id"]
            title = mkt["title"]
            category = mkt["category"]
            close_date = mkt["close_date"]

            # Get latest price snapshot for this market
            with get_session(self._engine) as session:
                snap_rows = session.execute(
                    select(
                        PriceSnapshot.yes_bid,
                        PriceSnapshot.yes_ask,
                        PriceSnapshot.last_price,
                        PriceSnapshot.volume,
                    )
                    .where(PriceSnapshot.market_id == market_id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                ).first()

            if snap_rows is None:
                continue

            yes_bid, yes_ask, last_price, volume = snap_rows

            # Run models
            models = registry.get_models_for(category)
            model_result = None
            for model in models:
                result = model.estimate(
                    market_id=market_id,
                    title=title,
                    current_price=last_price / 100.0,
                    engine=self._engine,
                )
                if result is not None:
                    model_result = result
                    break

            if model_result is None:
                continue

            # Calculate EV
            ev_result = calculate_ev(
                p_model=model_result.p_model,
                price_cents=last_price,
            )

            side = ev_result.recommended_side
            edge = ev_result.best_edge
            net_ev = ev_result.best_ev

            if abs(edge) < _BT_MIN_EDGE or net_ev <= 0:
                continue

            # Kelly sizing
            price_cents = last_price if side == "yes" else (100 - last_price)
            price = price_cents / 100.0

            if price <= 0 or price >= 1:
                continue

            p = model_result.p_model if side == "yes" else (1 - model_result.p_model)
            b = (1 - price) / price
            q = 1 - p
            full_kelly = max(0, (b * p - q) / b)
            fractional = full_kelly * _BT_KELLY_FRACTION
            dollars = bankroll * fractional
            max_trade = bankroll * _BT_MAX_SINGLE_TRADE_PCT
            dollars = min(dollars, max_trade)

            quantity = math.floor(dollars / price) if price > 0 else 0
            if quantity == 0:
                continue

            # Resolve: use final price > 50 as "resolved Yes"
            with get_session(self._engine) as session:
                final_snap = session.execute(
                    select(PriceSnapshot.last_price)
                    .where(PriceSnapshot.market_id == market_id)
                    .order_by(PriceSnapshot.timestamp.desc())
                    .limit(1)
                ).scalar()

            resolved_yes = (final_snap or 50) > 50
            if side == "yes":
                exit_price = 100 if resolved_yes else 0
                realized_pnl = (exit_price - price_cents) * quantity / 100.0
            else:
                exit_price = 0 if resolved_yes else 100
                realized_pnl = (price_cents - exit_price) * quantity / 100.0

            bankroll = round(bankroll + realized_pnl, 2)
            peak = max(peak, bankroll)
            if peak > 0:
                dd = (peak - bankroll) / peak * 100
                max_drawdown_pct = max(max_drawdown_pct, dd)

            trades.append({
                "run_id": run_id,
                "market_id": market_id,
                "side": side,
                "entry_price": price_cents,
                "exit_price": exit_price,
                "quantity": quantity,
                "p_model": model_result.p_model,
                "implied_prob": ev_result.implied_prob,
                "edge": edge,
                "net_ev": net_ev,
                "realized_pnl": realized_pnl,
                "resolved_at": close_date,
            })

        # Save trades and update run
        wins = len([t for t in trades if t["realized_pnl"] > 0])
        losses = len([t for t in trades if t["realized_pnl"] <= 0])
        total_pnl = sum(t["realized_pnl"] for t in trades)

        with get_session(self._engine) as session:
            for t in trades:
                session.add(BacktestTrade(**t))

            run = session.query(BacktestRun).get(run_id)
            run.final_bankroll = round(initial_bankroll + total_pnl, 2)
            run.total_trades = len(trades)
            run.wins = wins
            run.losses = losses
            run.total_pnl = round(total_pnl, 2)
            run.max_drawdown_pct = round(max_drawdown_pct, 2)
            run.status = "completed"
            session.commit()

        logger.info(
            f"Backtest {run_id} completed: {len(trades)} trades, "
            f"PnL ${total_pnl:.2f}, final ${bankroll:.2f}"
        )

        return run_id
