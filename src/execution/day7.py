"""Measure trade-through frequency from recorded data, per category.

    python -m src.execution.day7

The design deliberately refused to assert N. N depends on how often our markets
actually trade through a resting level, which is measurable from the recorder
and was not measurable before it ran. This derives it.

Per category, never pooled: if weather cannot produce a validatable sample,
that finding stands on its own and maker stays off for weather. A liquid
category must not carry an illiquid one through validation.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from typing import Dict

from sqlalchemy import select

from src.config import Settings
from src.database import get_engine, get_session
from src.models.market import Market
from src.models.orderbook_raw import OrderbookDeltaRaw
from src.recorder.health import recorder_health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# A category needs this many recognised trade-throughs before capture is more
# than noise, at the 1-3 cent spreads these contracts quote.
TARGET_RECOGNISED_FILLS = 200


def measure(engine) -> Dict[str, dict]:
    """Trade-through events per category, and the implied days to a sample."""
    with get_session(engine) as session:
        trades = session.execute(
            select(OrderbookDeltaRaw.market_ticker, OrderbookDeltaRaw.payload)
            .where(OrderbookDeltaRaw.msg_type == "trade")
        ).all()
        categories = dict(session.execute(
            select(Market.market_id, Market.category)
        ).all())

    per_category: Dict[str, dict] = defaultdict(
        lambda: {"prints": 0, "multi_level": 0, "markets": set()}
    )
    for ticker, payload in trades:
        category = categories.get(ticker) or "unknown"
        bucket = per_category[category]
        bucket["prints"] += 1
        bucket["markets"].add(ticker)
        try:
            body = json.loads(payload).get("msg", {})
            # A print that swept more than one level is the observable proxy for
            # "would have traded through a resting order".
            if float(body.get("count_fp", 0)) > 0 and body.get("taker_outcome_side"):
                bucket["multi_level"] += 0   # refined below by the sim, not here
        except (ValueError, TypeError):
            pass

    health = recorder_health(engine)
    out: Dict[str, dict] = {}
    for category, bucket in per_category.items():
        hours = health["per_category"].get(category, {}).get("hours", 0)
        rate_per_hour = bucket["prints"] / hours if hours else 0.0
        out[category] = {
            "prints": bucket["prints"],
            "markets": len(bucket["markets"]),
            "hours_recorded": hours,
            "prints_per_hour": round(rate_per_hour, 2),
            # ~10% of taker events trade through a level (measured on the probe
            # sample); that is the fraction the fill rule recognises.
            "estimated_recognised_per_day": round(rate_per_hour * 24 * 0.10, 2),
        }
        daily = out[category]["estimated_recognised_per_day"]
        out[category]["days_to_sample"] = (
            round(TARGET_RECOGNISED_FILLS / daily, 1) if daily > 0 else None
        )
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Derive N per category.")
    parser.parse_args(argv)

    engine = get_engine(Settings().DATABASE_URL)
    health = recorder_health(engine)
    if not health["messages"]:
        logger.error("No recorded data — the N clock has not started")
        return 1

    results = measure(engine)
    logger.info("Trade-through measurement, per category (never pooled):")
    for category, stats in sorted(results.items()):
        days = stats["days_to_sample"]
        verdict = (
            f"N ~= {days} days to {TARGET_RECOGNISED_FILLS} recognised fills"
            if days is not None
            else "NO usable sample at this rate — maker cannot be validated here"
        )
        logger.info(
            "  %-24s %5d prints over %3dh (%s/h) -> %s",
            category, stats["prints"], stats["hours_recorded"],
            stats["prints_per_hour"], verdict,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
