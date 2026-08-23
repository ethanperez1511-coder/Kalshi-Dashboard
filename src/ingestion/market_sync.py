from __future__ import annotations
import hashlib
import logging
from typing import List
from sqlalchemy import Engine, select
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
    is_in_scope,
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

    if not is_in_scope(km):
        # A temperature market for a station we do not model. Not a parse
        # failure — there is no fit for it and never was.
        return {
            "strike_direction": None, "strike_value": None,
            "strike_unit": None, "terms_status": TERMS_UNSUPPORTED,
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


def _content_hash(row: dict) -> str:
    """Digest of every field ingest writes.

    Covers ALL of them deliberately. Kalshi reworded the settlement clause of
    all seven temperature series on 2026-08-14 and changed nothing else; a
    comparison that skipped `rules` would have kept serving the old text
    forever and left the settlement guard verifying a string no longer on the
    market.
    """
    parts = [
        f"{key}={row.get(key)!r}"
        for key in sorted(row)
        if key not in ("id", "content_hash")
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _write_rows(session, inserts: List[dict], updates: List[dict]) -> None:
    if inserts:
        session.bulk_insert_mappings(Market, inserts)
    if updates:
        session.bulk_update_mappings(Market, updates)


def sync_markets(
    engine: Engine,
    kalshi_markets: List[KalshiMarket],
    series_ticker: str = "",
) -> int:
    """Upsert markets in bulk.

    This was a SELECT-then-INSERT/UPDATE per ticker. Against SQLite each is a
    function call; against Neon each is a network round-trip, and ~5,000 markets
    meant ~10,000 sequential round-trips per cycle. Now: one SELECT for the ids
    that already exist, then two bulk statements.

    Still a single transaction, so a mid-cycle kill rolls the whole sync back
    rather than leaving half a market table.
    """
    if not kalshi_markets:
        return 0

    # Collapse duplicates within the batch, last occurrence winning. The
    # previous SELECT-then-upsert loop tolerated a repeated ticker implicitly;
    # a bulk insert would violate the unique constraint instead.
    deduped = {km.ticker: km for km in kalshi_markets}
    kalshi_markets = list(deduped.values())

    tickers = [km.ticker for km in kalshi_markets]
    with get_session(engine) as session:
        # The hash comes back with the id, and it is 32 bytes against the ~700
        # bytes of title+rules it stands in for. Fetching the fields themselves
        # to decide whether to write them would cost the same transfer as
        # writing them.
        existing = {
            ticker: (row_id, content_hash)
            for row_id, ticker, content_hash in session.execute(
                select(Market.id, Market.market_id, Market.content_hash)
                .where(Market.market_id.in_(tickers))
            ).all()
        }

        inserts: List[dict] = []
        updates: List[dict] = []
        unchanged = 0
        for km in kalshi_markets:
            row = {
                "market_id": km.ticker,
                "title": km.title,
                "category": km.category,
                "close_date": km.close_time,
                "status": km.status,
                "rules": km.rules_primary,
                **_terms_fields(km),
            }
            if series_ticker:
                row["series_ticker"] = series_ticker
            row["content_hash"] = _content_hash(row)
            known = existing.get(km.ticker)
            if known is None:
                inserts.append(row)
                continue
            row_id, stored_hash = known
            if stored_hash == row["content_hash"]:
                # Identical to what is already stored. Writing it again would
                # move ~700 bytes to change nothing.
                unchanged += 1
                continue
            updates.append({**row, "id": row_id})

        _write_rows(session, inserts, updates)
        session.commit()

    logger.info(
        "Synced %d markets (%d new, %d changed, %d unchanged and skipped)",
        len(kalshi_markets), len(inserts), len(updates), unchanged,
    )
    return len(inserts)
