from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, func, select

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

logger = logging.getLogger(__name__)


def score_all_markets(
    engine: Engine, fee_rate: float = 0.01, deadline=None,
) -> List[Dict[str, Any]]:
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

    # --- Step 1: load open markets WITH their latest snapshot, in one query ---
    # This used to be one query for the markets and then one more PER MARKET for
    # its snapshot. Against SQLite that is a function call; against Neon every
    # one is a network round-trip, and at ~135k open markets it consumed the
    # entire 8-minute job budget before scoring began.
    #
    # The stale-snapshot guard means a market without a recent snapshot is
    # skipped anyway, so the cutoff is applied in SQL rather than in Python —
    # which also collapses the row count the loop has to walk.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_SNAPSHOT_AGE_MINUTES)
    with get_session(engine) as session:
        newest = (
            select(
                PriceSnapshot.market_id.label("market_id"),
                func.max(PriceSnapshot.id).label("snap_id"),
            )
            .group_by(PriceSnapshot.market_id)
            .subquery()
        )
        rows = session.execute(
            select(
                Market.market_id, Market.title, Market.category, Market.close_date,
                PriceSnapshot.yes_bid, PriceSnapshot.yes_ask,
                PriceSnapshot.last_price, PriceSnapshot.volume,
                PriceSnapshot.timestamp,
            )
            .join(newest, newest.c.market_id == Market.market_id)
            .join(PriceSnapshot, PriceSnapshot.id == newest.c.snap_id)
            .where(Market.status.in_(["open", "active"]))
            .where(PriceSnapshot.timestamp >= cutoff)
            # Deterministic, and ordered by the thing that predicts qualifying:
            # liquidity. Previously unordered, so if the budget cut the loop
            # short it truncated an arbitrary slice — a market might be scored
            # one cycle and skipped the next for no reason, and there was no
            # guarantee the liquid markets were reached at all.
            .order_by(PriceSnapshot.volume.desc(), Market.market_id)
        ).all()
        markets = [
            {
                "market_id": r[0], "title": r[1], "category": r[2],
                "close_date": r[3],
                "snapshot": (r[4], r[5], r[6], r[7], r[8]),
            }
            for r in rows
        ]
    logger.info("Scoring %d markets with fresh snapshots", len(markets))

    results: List[Dict[str, Any]] = []
    pending_opportunities: List[dict] = []

    for index, mkt in enumerate(markets):
        # Checked every 50 markets: often enough to stop promptly, rare
        # enough that the clock read is not itself the cost.
        if deadline is not None and index % 50 == 0 and deadline.expired():
            logger.warning(
                "Scoring stopped at %d/%d markets (budget) — the rest of "
                "the cycle continues", index, len(markets),
            )
            break
        market_id: str = mkt["market_id"]
        title: str = mkt["title"]
        category: str = mkt["category"]
        close_date: datetime = mkt["close_date"]

        # --- Step 2: snapshot already loaded with the market ---
        snap_row = mkt["snapshot"]

        if snap_row is None:
            continue

        yes_bid, yes_ask, last_price, volume, snap_ts = snap_row

        # Skip markets with no trade history
        if last_price == 0:
            continue

        # Stale data guard. The SQL cutoff above already excludes these; this
        # stays as a second line of defence so the invariant holds even if the
        # query is ever changed.
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
        # Skip price-derived models outright when they are gated off. They were
        # still being RUN — each querying the database per market — and their
        # result discarded by the gate immediately below. Behaviour-identical,
        # since a price-derived winner produced no Opportunity row either way.
        models = [
            m for m in registry.get_models_for(category)
            if TRADE_PRICE_DERIVED_MODELS or m.model_type != MODEL_TYPE_PRICE_DERIVED
        ]
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

        # --- Step 7: queue the Opportunity row (written in one batch below) ---
        pending_opportunities.append(_opportunity_row(
            engine=engine,
            market_id=market_id,
            ev_result=ev_result,
            model_result=model_result,
            model_name=winning_model_name,
            filter_result_status=filter_result.status,
            reasoning=model_result.reasoning,
        ))

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

    # One write for every scored market, rather than a SELECT + INSERT/UPDATE
    # + COMMIT each. That was three network round-trips per market against Neon.
    _flush_opportunities(engine, pending_opportunities)
    return results


def _opportunity_row(
    engine=None, market_id="", ev_result=None, model_result=None,
    model_name="", filter_result_status="", reasoning="",
) -> dict:
    """Plain values for one Opportunity row. Writes nothing."""
    return {
        "market_id": market_id,
        "p_model": ev_result.p_model,
        "implied_prob": ev_result.implied_prob,
        "edge": ev_result.best_edge,
        "net_ev": ev_result.best_ev,
        "recommended_side": ev_result.recommended_side,
        "confidence": model_result.confidence,
        "status": filter_result_status,
        "reasoning": reasoning,
        "model_name": model_name,
    }


def _flush_opportunities(engine: Engine, rows: List[dict]) -> None:
    """Upsert every scored opportunity in one read and two writes."""
    if not rows:
        return
    market_ids = [r["market_id"] for r in rows]
    with get_session(engine) as session:
        existing = {
            market_id: row_id
            for row_id, market_id in session.execute(
                select(Opportunity.id, Opportunity.market_id)
                .where(Opportunity.market_id.in_(market_ids))
            ).all()
        }
        inserts = [r for r in rows if r["market_id"] not in existing]
        updates = [
            {**r, "id": existing[r["market_id"]]}
            for r in rows if r["market_id"] in existing
        ]
        if inserts:
            session.bulk_insert_mappings(Opportunity, inserts)
        if updates:
            session.bulk_update_mappings(Opportunity, updates)
        session.commit()
