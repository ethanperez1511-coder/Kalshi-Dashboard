from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, select

from src.config import Settings
from src.database import get_session
from src.ev.calculator import calculate_ev
from src.ev.filter import TradeFilter
from src.modeling.base import MODEL_TYPE_PRICE_DERIVED
from src.modeling.odds_api import OddsClient
from src.modeling.registry import ModelRegistry
from src.trading_config import (
    TRADE_PRICE_DERIVED_MODELS,
    PRICE_DERIVED_MIN_EDGE,
    MAX_SPREAD_CENTS,
    MAX_SNAPSHOT_AGE_MINUTES,
    MAX_DAYS_TO_EXPIRY,
    ODDS_SPORT_KEYS,
    ODDS_TTL_MINUTES_OVERRIDE,
    PRESCREEN_BEFORE_MODELS,
)
from src.models.market import Market
from src.models.opportunity import Opportunity
from src.models.price import PriceSnapshot


def score_all_markets(engine: Engine, fee_rate: float = 0.01) -> List[Dict[str, Any]]:
    """Score all open markets and upsert Opportunity rows.

    Steps for each open market:
    1. Load the latest PriceSnapshot.
    2. Run models from the registry (first non-None result wins).
    3. Calculate EV and run TradeFilter.
    4. Upsert an Opportunity row.
    5. Return a list of result dicts.
    """
    settings = Settings()
    # Passing the engine is what makes the odds cache and quota ledger survive
    # the process — this pipeline is a fresh interpreter on every cron tick.
    # ttl_seconds=None lets the client derive a TTL that fits the monthly cap.
    odds_client = (
        OddsClient(
            settings.ODDS_API_KEY,
            sport_keys=[s.strip() for s in ODDS_SPORT_KEYS.split(",") if s.strip()],
            ttl_seconds=(
                ODDS_TTL_MINUTES_OVERRIDE * 60
                if ODDS_TTL_MINUTES_OVERRIDE > 0
                else None
            ),
            engine=engine,
        )
        if settings.ODDS_API_KEY
        else None
    )
    registry = ModelRegistry(odds_client=odds_client)
    trade_filter = TradeFilter(
        max_spread_cents=MAX_SPREAD_CENTS,
        max_hours_to_expiry=MAX_DAYS_TO_EXPIRY * 24,
    )

    # --- Step 1: load open markets as plain dicts ---
    with get_session(engine) as session:
        rows = session.execute(
            select(
                Market.market_id,
                Market.title,
                Market.category,
                Market.close_date,
            ).where(Market.status.in_(["open", "active"]))
        ).all()
        markets = [
            {
                "market_id": r[0],
                "title": r[1],
                "category": r[2],
                "close_date": r[3],
            }
            for r in rows
        ]

    results: List[Dict[str, Any]] = []

    for mkt in markets:
        market_id: str = mkt["market_id"]
        title: str = mkt["title"]
        category: str = mkt["category"]
        close_date: datetime = mkt["close_date"]

        # --- Step 2: get latest PriceSnapshot as plain values ---
        with get_session(engine) as session:
            snap_row = session.execute(
                select(
                    PriceSnapshot.yes_bid,
                    PriceSnapshot.yes_ask,
                    PriceSnapshot.last_price,
                    PriceSnapshot.volume,
                    PriceSnapshot.timestamp,
                )
                .where(PriceSnapshot.market_id == market_id)
                .order_by(PriceSnapshot.timestamp.desc())
                .limit(1)
            ).first()

        if snap_row is None:
            continue

        yes_bid, yes_ask, last_price, volume, snap_ts = snap_row

        # Skip markets with no trade history
        if last_price == 0:
            continue

        # Stale data guard: a market with no fresh snapshot has closed early or
        # dropped out of the ingest feed — its price is fiction, never trade it.
        if snap_ts is not None:
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - snap_ts).total_seconds() / 60.0
            if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
                continue

        # --- Step 2b: market-only gates, computed before any model runs ---
        now = datetime.now(timezone.utc)
        if close_date.tzinfo is None:
            close_date = close_date.replace(tzinfo=timezone.utc)
        hours_to_expiry = max(0.0, (close_date - now).total_seconds() / 3600.0)
        spread_cents = yes_ask - yes_bid

        # Optional: skip model dispatch for markets that cannot qualify anyway.
        # Model dispatch can spend a metered odds request, and spending it on a
        # market that fails a liquidity gate is pure waste. prescreen() only
        # duplicates gates evaluate() already applies, so no decision changes —
        # but the market gets no Opportunity row, so this is opt-in.
        if PRESCREEN_BEFORE_MODELS and not trade_filter.prescreen(
            daily_volume=volume,
            bid_ask_spread_cents=spread_cents,
            hours_to_expiry=hours_to_expiry,
        ):
            continue

        # --- Step 3: run models (first non-None wins) ---
        models = registry.get_models_for(category)
        model_result = None
        winning_model_name: str = "Unknown"
        winning_model_type: str = MODEL_TYPE_PRICE_DERIVED
        for model in models:
            result = model.estimate(
                market_id=market_id,
                title=title,
                current_price=last_price / 100.0,
                engine=engine,
            )
            if result is not None:
                model_result = result
                winning_model_name = type(model).__name__
                winning_model_type = model.model_type
                break

        if model_result is None:
            continue

        # Gate price-derived models unless explicitly enabled
        if winning_model_type == MODEL_TYPE_PRICE_DERIVED and not TRADE_PRICE_DERIVED_MODELS:
            continue

        # --- Step 4: calculate EV ---
        ev_result = calculate_ev(
            p_model=model_result.p_model,
            price_cents=last_price,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
        )

        # --- Step 6: run TradeFilter (hours_to_expiry/spread computed above) ---
        # Price-derived models use a higher edge threshold
        min_edge_override = PRICE_DERIVED_MIN_EDGE if winning_model_type == MODEL_TYPE_PRICE_DERIVED else None
        filter_result = trade_filter.evaluate(
            ev_result=ev_result,
            confidence=model_result.confidence,
            daily_volume=volume,
            bid_ask_spread_cents=spread_cents,
            hours_to_expiry=hours_to_expiry,
            min_edge_override=min_edge_override,
        )

        # --- Step 7: upsert Opportunity ---
        _upsert_opportunity(
            engine=engine,
            market_id=market_id,
            ev_result=ev_result,
            model_result=model_result,
            model_name=winning_model_name,
            filter_result_status=filter_result.status,
            reasoning=model_result.reasoning,
        )

        result_dict = {
            "market_id": market_id,
            "p_model": model_result.p_model,
            "implied_prob": ev_result.implied_prob,
            "edge": ev_result.edge,
            "net_ev": ev_result.net_ev,
            "recommended_side": ev_result.recommended_side,
            "confidence": model_result.confidence,
            "status": filter_result.status,
            "reasoning": model_result.reasoning,
            "model_name": winning_model_name,
            "model_type": winning_model_type,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
        }
        results.append(result_dict)

    return results


def _upsert_opportunity(
    engine: Engine,
    market_id: str,
    ev_result: Any,
    model_result: Any,
    model_name: str,
    filter_result_status: str,
    reasoning: str,
) -> None:
    """Insert or update an Opportunity row for the given market."""

    with get_session(engine) as session:
        existing: Optional[Opportunity] = (
            session.query(Opportunity).filter_by(market_id=market_id).first()
        )
        now = datetime.now(timezone.utc)

        if existing is not None:
            existing.p_model = model_result.p_model
            existing.implied_prob = ev_result.implied_prob
            existing.edge = ev_result.edge
            existing.net_ev = ev_result.net_ev
            existing.recommended_side = ev_result.recommended_side
            existing.confidence = model_result.confidence
            existing.status = filter_result_status
            existing.reasoning = reasoning
            existing.model_name = model_name
            existing.scored_at = now
        else:
            opp = Opportunity(
                market_id=market_id,
                p_model=model_result.p_model,
                implied_prob=ev_result.implied_prob,
                edge=ev_result.edge,
                net_ev=ev_result.net_ev,
                recommended_side=ev_result.recommended_side,
                confidence=model_result.confidence,
                status=filter_result_status,
                reasoning=reasoning,
                model_name=model_name,
                scored_at=now,
            )
            session.add(opp)
        session.commit()
