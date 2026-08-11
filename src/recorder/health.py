"""Recorder health, per category, for the daily digest.

The N clock runs on recorded book hours, not calendar days, and it runs
separately per category: a liquid category must not carry an illiquid one
through validation. Weather contracts are thin — one probed market produced no
deltas in fifteen seconds — so weather may take far longer than sports to
accumulate a usable sample, or may never get there. Showing coverage pooled
would hide exactly that.

Hours of coverage is measured as distinct recorded hours, not as
last-minus-first. An hourly job that ran twice a week would otherwise report a
full week of "coverage" containing two hours of data.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap


def _category_of(engine: Engine, tickers: List[str]) -> Dict[str, str]:
    if not tickers:
        return {}
    with get_session(engine) as session:
        rows = session.execute(
            select(Market.market_id, Market.category)
            .where(Market.market_id.in_(tickers))
        ).all()
    return {ticker: (category or "unknown") for ticker, category in rows}


def recorder_health(engine: Engine, now: dt.datetime = None) -> Dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)

    with get_session(engine) as session:
        total = session.execute(
            select(func.count(OrderbookDeltaRaw.id))
        ).scalar() or 0
        gaps = session.execute(select(func.count(OrderbookGap.id))).scalar() or 0
        tickers = [
            row[0] for row in session.execute(
                select(OrderbookDeltaRaw.market_ticker).distinct()
            ).all()
        ]
        rows = session.execute(
            select(OrderbookDeltaRaw.market_ticker, OrderbookDeltaRaw.received_at)
        ).all()
        last = session.execute(
            select(func.max(OrderbookDeltaRaw.received_at))
        ).scalar()

    categories = _category_of(engine, tickers)

    # Distinct recorded hours per category — the honest measure of coverage.
    hours: Dict[str, set] = {}
    counts: Dict[str, int] = {}
    for ticker, received in rows:
        category = categories.get(ticker, "unknown")
        counts[category] = counts.get(category, 0) + 1
        if received is not None:
            stamp = received if received.tzinfo else received.replace(tzinfo=dt.timezone.utc)
            hours.setdefault(category, set()).add(stamp.strftime("%Y-%m-%dT%H"))

    staleness_hours = None
    if last is not None:
        stamp = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
        staleness_hours = (now - stamp).total_seconds() / 3600.0

    return {
        "messages": total,
        "gaps": gaps,
        "markets": len(tickers),
        "per_category": {
            category: {"messages": counts.get(category, 0), "hours": len(bucket)}
            for category, bucket in sorted(hours.items())
        },
        "hours_since_last_message": staleness_hours,
    }


def format_recorder_health(data: Dict[str, Any]) -> str:
    if not data.get("messages"):
        return "🎙 Recorder: NO DATA — the N clock has not started"

    lines = [
        f"🎙 Recorder: {data['messages']} msgs, {data['markets']} markets, "
        f"{data['gaps']} seq gaps"
    ]
    for category, stats in data["per_category"].items():
        lines.append(
            f"   {category}: {stats['hours']}h coverage, {stats['messages']} msgs"
        )
    stale = data.get("hours_since_last_message")
    if stale is not None and stale > 2:
        lines.append(f"   ⚠️ nothing recorded for {stale:.1f}h — recorder may be down")
    return "\n".join(lines)
