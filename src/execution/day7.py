"""Derive N per category from recorded data, and label what is measured.

The design refused to assert N. It depends on how often our markets actually
trade through a resting level, which was not measurable before the recorder ran.

Everything here is tagged MEASURED or CARRIED. A carried number is an
assumption inherited from the probe sample, which was taken on liquid markets
and is a poor guide to thin ones — weather especially. Carried numbers are
replaced by measured ones as soon as enough data exists, and the report says
which is which every time, rather than letting an assumption harden into a fact
by being repeated.

Per category, never pooled: if weather cannot produce a validatable sample,
that finding stands on its own and maker stays off for weather.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections import defaultdict
from typing import Dict, Optional

from sqlalchemy import Engine, func, select

from src.config import Settings
from src.database import get_engine, get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.recorder.health import recorder_health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Recognised fills needed before capture is more than noise at the 1-3 cent
# spreads these contracts quote.
TARGET_RECOGNISED_FILLS = 200

# From the probe: 121 of 1200 taker events touched 2+ price levels. Liquid
# markets only. CARRIED until a category measures its own.
PROBE_MULTI_LEVEL_RATE = 0.10

# Below this many prints a measured rate is itself noise, so the carried one
# is still the better estimate — and is labelled as carried.
MIN_PRINTS_TO_MEASURE = 200

# Below this many recorded hours, projecting a daily rate is arithmetic, not
# evidence: one print in one hour extrapolates to a viable-looking N. Emit no
# verdict rather than a confident-looking number built on nothing.
MIN_HOURS_TO_PROJECT = 24


def measure(engine: Engine) -> Dict[str, dict]:
    """Trade-through frequency per category, measured where possible.

    Kalshi prints each maker counterparty separately, so one sweep arrives as
    several prints sharing a timestamp. Counting distinct prices within a
    timestamp is what makes the multi-level rate measurable at all.
    """
    with get_session(engine) as session:
        rows = session.execute(
            select(
                OrderbookDeltaRaw.market_ticker,
                OrderbookDeltaRaw.ts_ms,
                OrderbookDeltaRaw.payload,
            ).where(OrderbookDeltaRaw.msg_type == "trade")
        ).all()
        categories = dict(session.execute(
            select(Market.market_id, Market.category)
        ).all())

    sweeps: Dict[str, Dict[tuple, set]] = defaultdict(lambda: defaultdict(set))
    prints: Dict[str, int] = defaultdict(int)
    markets: Dict[str, set] = defaultdict(set)

    for ticker, ts_ms, payload in rows:
        category = categories.get(ticker) or "unknown"
        prints[category] += 1
        markets[category].add(ticker)
        try:
            body = json.loads(payload).get("msg", {})
        except (ValueError, TypeError):
            continue
        sweeps[category][(ticker, ts_ms)].add(str(body.get("yes_price_dollars")))

    health = recorder_health(engine)
    out: Dict[str, dict] = {}

    for category, count in prints.items():
        groups = sweeps[category]
        multi = sum(1 for levels in groups.values() if len(levels) > 1)
        enough = count >= MIN_PRINTS_TO_MEASURE
        rate = (multi / len(groups)) if (groups and enough) else PROBE_MULTI_LEVEL_RATE

        hours = health["per_category"].get(category, {}).get("hours", 0)
        per_hour = (count / hours) if hours else 0.0
        recognised_per_day = per_hour * 24 * rate

        projectable = hours >= MIN_HOURS_TO_PROJECT
        out[category] = {
            "prints": count,
            "markets": len(markets[category]),
            "hours_recorded": hours,
            "prints_per_hour": round(per_hour, 2),
            "sweeps": len(groups),
            "multi_level_sweeps": multi,
            "multi_level_rate": round(rate, 4),
            "rate_source": "MEASURED" if enough else "CARRIED (probe, liquid markets)",
            "projectable": projectable,
            "recognised_fills_per_day": (
                round(recognised_per_day, 2) if projectable else None
            ),
            "days_to_sample": (
                round(TARGET_RECOGNISED_FILLS / recognised_per_day, 1)
                if projectable and recognised_per_day > 0 else None
            ),
        }
    return out


def clock_start(engine: Engine) -> Optional[dt.datetime]:
    """When the first delta landed — the day-7 clock's actual start."""
    with get_session(engine) as session:
        return session.execute(
            select(func.min(OrderbookDeltaRaw.received_at))
        ).scalar()


def format_report(results: Dict[str, dict], start: Optional[dt.datetime]) -> str:
    lines = ["Day-7 trade-through measurement (per category, never pooled)"]
    if start is not None:
        stamp = start if start.tzinfo else start.replace(tzinfo=dt.timezone.utc)
        elapsed = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 86400.0
        lines.append(
            f"  clock started {stamp:%Y-%m-%d %H:%M UTC} ({elapsed:.1f} days ago)"
        )
    lines.append("")

    for category, stats in sorted(results.items()):
        lines.append(f"  {category}")
        lines.append(
            f"    MEASURED: {stats['prints']} prints over "
            f"{stats['hours_recorded']}h across {stats['markets']} markets "
            f"({stats['prints_per_hour']}/h)"
        )
        lines.append(
            f"    multi-level rate {stats['multi_level_rate']:.1%} "
            f"[{stats['rate_source']}] from {stats['multi_level_sweeps']}/"
            f"{stats['sweeps']} sweeps"
        )
        days = stats["days_to_sample"]
        if not stats["projectable"]:
            lines.append(
                f"    VERDICT: only {stats['hours_recorded']}h recorded "
                f"(need {MIN_HOURS_TO_PROJECT}h) — too little to project from. "
                f"No N derived."
            )
        elif days is None:
            lines.append(
                "    VERDICT: no usable sample at this rate — maker capture "
                "cannot be validated for this category"
            )
        else:
            lines.append(
                f"    VERDICT: N ~= {days} days to {TARGET_RECOGNISED_FILLS} "
                f"recognised fills ({stats['recognised_fills_per_day']}/day)"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Derive N per category.")
    parser.parse_args(argv)

    engine = get_engine(Settings().DATABASE_URL)
    if not recorder_health(engine)["messages"]:
        logger.error(
            "No recorded book data — the day-7 clock has not started. Nothing "
            "here can be derived until the recorder runs."
        )
        return 1

    print(format_report(measure(engine), clock_start(engine)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
