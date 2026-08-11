from __future__ import annotations
import logging
from typing import List
from sqlalchemy import Engine
from src.database import get_session
from src.kalshi.schemas import KalshiMarket
from src.models.market import (
    Market,
    TERMS_NOT_APPLICABLE,
    TERMS_PARSED,
    TERMS_UNPARSED,
    TERMS_UNSUPPORTED,
)
from src.weather.terms import (
    is_temperature_market,
    is_unsupported_type,
    parse_contract_terms,
)

logger = logging.getLogger(__name__)


def _terms_fields(km: KalshiMarket) -> dict:
    """Threshold terms for a market, or an explicit record that it has none.

    Parsed once at ingest and stored, so a contract we cannot read becomes a
    countable state rather than something rediscovered — or worse, guessed at —
    every time the scorer runs.
    """
    if not is_temperature_market(km):
        return {
            "strike_direction": None, "strike_value": None,
            "strike_unit": None, "terms_status": TERMS_NOT_APPLICABLE,
        }

    if is_unsupported_type(km):
        # Readable, just not modelled yet — not a parser failure.
        return {
            "strike_direction": None, "strike_value": None,
            "strike_unit": None, "terms_status": TERMS_UNSUPPORTED,
        }

    terms = parse_contract_terms(km)
    if terms is None:
        logger.warning(
            "Weather contract %s could not be read (strike_type=%r, floor=%s, "
            "cap=%s) — marked unpriceable",
            km.ticker, km.strike_type, km.floor_strike, km.cap_strike,
        )
        return {
            "strike_direction": None, "strike_value": None,
            "strike_unit": None, "terms_status": TERMS_UNPARSED,
        }

    return {
        "strike_direction": terms.direction,
        "strike_value": terms.threshold,
        "strike_unit": terms.unit,
        "terms_status": TERMS_PARSED,
    }


def sync_markets(
    engine: Engine,
    kalshi_markets: List[KalshiMarket],
    series_ticker: str = "",
) -> int:
    new_count = 0
    with get_session(engine) as session:
        for km in kalshi_markets:
            fields = _terms_fields(km)
            existing = session.query(Market).filter_by(market_id=km.ticker).first()
            if existing:
                existing.title = km.title
                existing.category = km.category
                existing.status = km.status
                existing.close_date = km.close_time
                existing.rules = km.rules_primary
                for key, value in fields.items():
                    setattr(existing, key, value)
                if series_ticker:
                    existing.series_ticker = series_ticker
            else:
                market = Market(
                    market_id=km.ticker,
                    title=km.title,
                    category=km.category,
                    close_date=km.close_time,
                    status=km.status,
                    rules=km.rules_primary,
                    series_ticker=series_ticker or None,
                    **fields,
                )
                session.add(market)
                new_count += 1
        session.commit()
    logger.info(f"Synced {len(kalshi_markets)} markets ({new_count} new)")
    return new_count
