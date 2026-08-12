from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, func, select

from src.config import Settings
from src.database import get_session
from src.ev.calculator import calculate_ev
from src.ev.filter import TradeFilter
from src.ev.funnel import ScoreFunnel
from src.modeling.base import MODEL_TYPE_INDEPENDENT, MODEL_TYPE_PRICE_DERIVED
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
    funnel: Optional[ScoreFunnel] = None,
) -> List[Dict[str, Any]]:
    """Score all open markets and upsert Opportunity rows.

    Steps for each open market:
    1. Load the latest PriceSnapshot.
    2. Run models from the registry (first non-None result wins).
    3. Calculate EV and run TradeFilter.
    4. Upsert an Opportunity row.
    5. Return a list of result dicts.

    Pass a `ScoreFunnel` to have every market attributed to the gate that
    dropped it. The counters are a strict partition of the open universe, so
    a starved scorable set can be told apart from a misfiring gate without
    adding a log line after the fact.
    """
    funnel = funnel if funnel is not None else ScoreFunnel()
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
        # Counted separately because the join below silently drops markets with
        # no fresh snapshot, and "the feed stopped" and "nothing is scorable"
        # produce the same scored count without it.
        funnel.open_markets = session.execute(
            select(func.count()).select_from(Market)
            .where(Market.status.in_(["open", "active"]))
        ).scalar_one()
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
    funnel.with_fresh_snapshot = len(markets)
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
            funnel.budget_skipped = len(markets) - index
            break
        market_id: str = mkt["market_id"]
        title: str = mkt["title"]
        category: str = mkt["category"]
        close_date: datetime = mkt["close_date"]

        # --- Step 2: snapshot already loaded with the market ---
        snap_row = mkt["snapshot"]

        if snap_row is None:
            # Unreachable through the inner join above, but counted rather than
            # dropped: an uncounted `continue` is exactly what makes a funnel
            # stop balancing when the query is next changed.
            funnel.with_fresh_snapshot -= 1
            continue

        yes_bid, yes_ask, last_price, volume, snap_ts = snap_row

        # Skip markets with no trade history
        if last_price == 0:
            funnel.no_last_price += 1
            continue

        # Stale data guard. The SQL cutoff above already excludes these; this
        # stays as a second line of defence so the invariant holds even if the
        # query is ever changed.
        if snap_ts is not None:
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - snap_ts).total_seconds() / 60.0
            if age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
                # The SQL cutoff already excluded these, so reaching here means
                # the row aged out between the query and now. Counted with the
                # snapshot bucket so the partition still closes.
                funnel.with_fresh_snapshot -= 1
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
            funnel.prescreen_rejected += 1
            continue

        # --- Step 3: run models (first non-None wins) ---
        # Skip price-derived models outright when they are gated off. They were
        # still being RUN — each querying the database per market — and their
        # result discarded by the gate immediately below. Behaviour-identical,
        # since a price-derived winner produced no Opportunity row either way.
        models = [
            m for m in registry.get_models_for(category, market_id)
            if TRADE_PRICE_DERIVED_MODELS or m.model_type != MODEL_TYPE_PRICE_DERIVED
        ]
        if not any(m.model_type == MODEL_TYPE_INDEPENDENT for m in models):
            funnel.no_independent_model += 1
        model_result = None
        winning_model_name: str = "Unknown"
        winning_model_type: str = MODEL_TYPE_PRICE_DERIVED
        for model in models:
            funnel.model_attempts[type(model).__name__] += 1
            result = model.estimate(
                market_id=market_id,
                title=title,
                current_price=last_price / 100.0,
                engine=engine,
            )
            if result is not None:
                funnel.model_estimates[type(model).__name__] += 1
                model_result = result
                winning_model_name = type(model).__name__
                winning_model_type = model.model_type
                break

        if model_result is None:
            funnel.no_model_estimate += 1
            continue

        # Gate price-derived models unless explicitly enabled
        if winning_model_type == MODEL_TYPE_PRICE_DERIVED and not TRADE_PRICE_DERIVED_MODELS:
            funnel.price_derived_gated += 1
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
        funnel.scored += 1
        results.append(result_dict)

    # Whether the metered feed was reachable and what it cost. Zero sports
    # markets scoring during the season is a red flag for the source, not the
    # market, and only the ledger can tell those apart.
    if odds_client is not None:
        from src.modeling.odds_store import month_key
        try:
            month = month_key(datetime.now(timezone.utc))
            ledger = getattr(odds_client, "_ledger", None)
            funnel.odds_state = {
                "key_set": True,
                "quota_dead": bool(odds_client.quota_dead),
                "used": ledger.used(month, "the_odds_api") if ledger else None,
                "remaining": ledger.remaining(month, "the_odds_api") if ledger else None,
                "games_loaded": sum(
                    len(getattr(m, "_games", None) or [])
                    for m in registry.all_models
                ),
            }
        except Exception:
            logger.warning("Could not read odds quota state", exc_info=True)
    else:
        funnel.odds_state = {"key_set": False}

    # Models that track their own refusal reasons report them here. Without
    # this, "WeatherModel: 28 dispatched, 0 estimated" says something is wrong
    # but not which of its four independent gates fired.
    for model in registry.all_models:
        funnel.record_refusals(type(model).__name__, getattr(model, "refusals", None))
    if not funnel.balances():
        logger.error(
            "Score funnel does not balance: %d open, %d attributed — an "
            "uncounted rejection path exists",
            funnel.open_markets, funnel.attributed(),
        )

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
