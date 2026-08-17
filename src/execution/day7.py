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
from src.recorder.health import is_live, recorder_health
from src.report_guard import publish_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Recognised fills needed before capture is more than noise at the 1-3 cent
# spreads these contracts quote.
TARGET_RECOGNISED_FILLS = 200

# Below this many prints a measured rate is itself noise. There is no fallback:
# the probe rate (0.10, from 121 of 1200 taker events on LIQUID markets) was
# retired on 2026-08-17. Carrying a rate measured on one bucket into another is
# the cross-bucket carry the no-pooling ruling exists to forbid — it lets a
# liquid bucket vouch for a thin one through a constant. A bucket below this
# floor now gets no rate and no N at all.
MIN_PRINTS_TO_MEASURE = 200

# Below this many recorded hours, projecting a daily rate is arithmetic, not
# evidence: one print in one hour extrapolates to a viable-looking N. Emit no
# verdict rather than a confident-looking number built on nothing.
MIN_HOURS_TO_PROJECT = 24


_REGISTRY = None


def scope_for_market(market_id: str, category: str) -> str:
    """Which model claims this market — the axis validation actually needs.

    NOT Kalshi's category. The first day-7 report put 5,547 prints into one
    bucket called "General", which is the same label that made WeatherModel
    unreachable until dispatch moved to claimed scope. A blended bucket cannot
    support "maker validated for weather" or "for sports"; it supports nothing.

    `ModelRegistry.get_models_for` is imported rather than re-expressed, so the
    buckets the maker rule is validated over are exactly the buckets it would
    execute in. The fallback ConsensusModel is excluded deliberately: it claims
    everything, so counting it would rebuild the single blended bucket under a
    different name.
    """
    global _REGISTRY
    if _REGISTRY is None:
        from src.modeling.registry import ModelRegistry

        _REGISTRY = ModelRegistry()

    from src.modeling.models.consensus import ConsensusModel

    claiming = [
        type(m).__name__
        for m in _REGISTRY.get_models_for(category or "", market_id or "")
        if not isinstance(m, ConsensusModel)
    ]
    if not claiming:
        return "unclaimed"

    model = claiming[0]

    # A model whose scope IS a series map is split by series, because the
    # maker allow-list is per series and the evidence must never be coarser
    # than the switch. LAX carries 59% of the weather tape; a model-level pass
    # would license Denver on Los Angeles's liquidity, which is the same
    # failure as validating weather on Kalshi's "General" bucket, one layer
    # further down.
    from src.weather.stations import station_for_market

    station = station_for_market(market_id or "")
    if station is not None:
        return f"{model}:{station.series_ticker}"
    return model


def _series_of(market_id: str) -> str:
    return (market_id or "").split("-", 1)[0].upper()


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
                OrderbookDeltaRaw.received_at,
            ).where(OrderbookDeltaRaw.msg_type == "trade")
        ).all()
        facts = {
            market_id: (category, close_date)
            for market_id, category, close_date in session.execute(
                select(Market.market_id, Market.category, Market.close_date)
            ).all()
        }

    sweeps: Dict[str, Dict[tuple, set]] = defaultdict(lambda: defaultdict(set))
    prints: Dict[str, int] = defaultdict(int)
    markets: Dict[str, set] = defaultdict(set)
    by_series: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    skipped_not_live = 0

    for ticker, ts_ms, payload, received_at in rows:
        raw_category, close_date = facts.get(ticker, (None, None))
        # A print recorded after its market closed says nothing about how often
        # OUR markets trade through a resting level — it describes a book that
        # no longer exists. Counting it shortens the day-7 date on a sample
        # that cannot validate anything. Same rule as the coverage clock, from
        # the same function, so hours and prints always describe one sample.
        if not is_live(received_at, close_date):
            skipped_not_live += 1
            continue
        category = scope_for_market(ticker, raw_category or "")
        prints[category] += 1
        markets[category].add(ticker)
        by_series[category][_series_of(ticker)] += 1
        try:
            body = json.loads(payload).get("msg", {})
        except (ValueError, TypeError):
            continue
        sweeps[category][(ticker, ts_ms)].add(str(body.get("yes_price_dollars")))

    # Same scope function for the hours, so hours and prints describe one
    # population. A ratio of two different populations is not a rate.
    health = recorder_health(engine, scope_of=scope_for_market)
    if skipped_not_live:
        logger.warning(
            "Excluded %d trade prints recorded after their market closed — "
            "they are not evidence about a live book",
            skipped_not_live,
        )
    out: Dict[str, dict] = {}

    for category, count in prints.items():
        groups = sweeps[category]
        multi = sum(1 for levels in groups.values() if len(levels) > 1)
        enough = count >= MIN_PRINTS_TO_MEASURE and bool(groups)
        rate = (multi / len(groups)) if enough else None

        hours = health["per_category"].get(category, {}).get("hours", 0)
        per_hour = (count / hours) if hours else 0.0
        recognised_per_day = (per_hour * 24 * rate) if rate is not None else None

        projectable = hours >= MIN_HOURS_TO_PROJECT and rate is not None
        out[category] = {
            "prints": count,
            "markets": len(markets[category]),
            "hours_recorded": hours,
            "prints_per_hour": round(per_hour, 2),
            "sweeps": len(groups),
            "multi_level_sweeps": multi,
            "multi_level_rate": round(rate, 4) if rate is not None else None,
            "rate_source": "MEASURED" if enough else "UNMEASURED",
            "by_series": dict(by_series[category]),
            "projectable": projectable,
            "recognised_fills_per_day": (
                round(recognised_per_day, 2)
                if projectable and recognised_per_day is not None else None
            ),
            "days_to_sample": (
                round(TARGET_RECOGNISED_FILLS / recognised_per_day, 1)
                if projectable and recognised_per_day else None
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
    lines = [
        "Day-7 trade-through measurement (per CLAIMING MODEL, never pooled)"
    ]
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
        rate = stats["multi_level_rate"]
        if rate is None:
            lines.append(
                f"    multi-level rate UNMEASURED — {stats['prints']} prints "
                f"< {MIN_PRINTS_TO_MEASURE} floor. No rate is borrowed from "
                f"another bucket: that would be pooling."
            )
        else:
            lines.append(
                f"    multi-level rate {rate:.1%} "
                f"[{stats['rate_source']}] from {stats['multi_level_sweeps']}/"
                f"{stats['sweeps']} sweeps"
            )
        detail = stats.get("by_series") or {}
        if len(detail) > 1:
            lines.append(
                "    by series: "
                + ", ".join(
                    f"{series}={count}"
                    for series, count in sorted(detail.items(), key=lambda kv: -kv[1])
                )
            )
        days = stats["days_to_sample"]
        if rate is None:
            lines.append(
                f"    VERDICT: no measured rate for this bucket — no N derived."
            )
        elif not stats["projectable"]:
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
        return publish_report(
            "Day-7 measurement",
            "No recorded book data — the day-7 clock has not started. Nothing "
            "here can be derived until the recorder runs.",
            substantive=False,
        )

    results = measure(engine)
    # `format_report` renders its header whether or not any category made it
    # through, so the text alone cannot distinguish a healthy run from one that
    # measured nothing. The result set decides.
    return publish_report(
        "Day-7 measurement",
        format_report(results, clock_start(engine)),
        substantive=bool(results),
    )


if __name__ == "__main__":
    sys.exit(main())
