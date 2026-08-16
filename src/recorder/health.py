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

And an hour only counts if there was a live market on the other end. The
recorder's subscribe list was built from Opportunity rows that nothing
invalidates, so on 2026-08-16 it was still taping KXHIGH*-26AUG13 contracts
that had settled three days earlier. Those rows arrive, and used to count, but
a settled market's book is empty by construction: counting them pulls the day-7
validation date FORWARD, which is the direction that costs money — it would
declare the maker-fill rule validated on a sample of dead books.

So every row is classified against its own market's close date:

  live          recorded before the market closed — the only rows that count
  dead          recorded after it closed
  unattributed  blank ticker, or no `markets` row to check against

Dead and unattributed rows are reported, not deleted. Their share is the answer
to "how much of the record was ever real", and hiding it would replace a known
overcount with an unknown one.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw, OrderbookGap


def _market_facts(engine: Engine, tickers: List[str]) -> Dict[str, tuple]:
    """(category, close_date) per ticker. Absent means unattributable."""
    if not tickers:
        return {}
    with get_session(engine) as session:
        rows = session.execute(
            select(Market.market_id, Market.category, Market.close_date)
            .where(Market.market_id.in_(tickers))
        ).all()
    return {
        ticker: ((category or "unknown"), close_date)
        for ticker, category, close_date in rows
    }


def _aware(stamp: dt.datetime) -> dt.datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def is_live(received_at, close_date) -> bool:
    """Was this row recorded while its market was still open?

    The single definition of liveness, shared by the coverage clock and by the
    day-7 measurement. Two copies of this rule would let the hours and the
    prints disagree about which sample they describe, and the resulting N would
    belong to neither.

    Unknown is not live: a missing close date or a missing market row means we
    cannot show the book existed, and coverage has to be shown, not assumed.
    """
    if received_at is None or close_date is None:
        return False
    return _aware(received_at) < _aware(close_date)


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

    facts = _market_facts(engine, tickers)

    # Distinct recorded hours per category — the honest measure of coverage,
    # counted from live rows only.
    hours: Dict[str, set] = {}
    counts: Dict[str, int] = {}
    live = dead = unattributed = 0
    dead_markets: set = set()

    for ticker, received in rows:
        known = facts.get(ticker)
        if not ticker or known is None or known[1] is None or received is None:
            unattributed += 1
            continue

        category, close_date = known
        if not is_live(received, close_date):
            dead += 1
            dead_markets.add(ticker)
            continue

        live += 1
        counts[category] = counts.get(category, 0) + 1
        hours.setdefault(category, set()).add(
            _aware(received).strftime("%Y-%m-%dT%H")
        )

    staleness_hours = None
    if last is not None:
        staleness_hours = (now - _aware(last)).total_seconds() / 3600.0

    return {
        "messages": total,
        "gaps": gaps,
        "markets": len(tickers),
        "per_category": {
            category: {"messages": counts.get(category, 0), "hours": len(bucket)}
            for category, bucket in sorted(hours.items())
        },
        "liveness": {
            "live": live,
            "dead": dead,
            "unattributed": unattributed,
            "dead_markets": len(dead_markets),
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

    liveness = data.get("liveness") or {}
    wasted = liveness.get("dead", 0) + liveness.get("unattributed", 0)
    if wasted:
        share = 100.0 * wasted / data["messages"]
        lines.append(
            f"   ⚠️ {wasted} of {data['messages']} msgs ({share:.0f}%) are NOT "
            f"coverage: {liveness.get('dead', 0)} recorded after close across "
            f"{liveness.get('dead_markets', 0)} dead markets, "
            f"{liveness.get('unattributed', 0)} unattributable"
        )

    stale = data.get("hours_since_last_message")
    if stale is not None and stale > 2:
        lines.append(f"   ⚠️ nothing recorded for {stale:.1f}h — recorder may be down")
    return "\n".join(lines)
