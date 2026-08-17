"""Which series may post maker orders. Two conditions to enable, one to kill.

    TRADING_MAKER_ENABLED         global master, default False
    TRADING_MAKER_ENABLED_SERIES  per-series allow-list, default empty

A series is maker ONLY if the master is on AND the series appears in the list.
Everything else is taker, including a series with perfect evidence that nobody
added. Absence is never consent.

Per series and not per model because LAX carries 59% of the weather tape. A
model-level switch would license Denver on Los Angeles's liquidity — the same
failure as validating weather inside Kalshi's "General" bucket, one layer
further down. The day-7 bars are measured at this same granularity so the
switch can never be finer than the evidence behind it.

Matched on the whole series token, case-insensitive: "KXHIGH" as a prefix rule
would enable all seven cities at once.

THE DEFAULT LIST MUST STAY EMPTY. `_env_str` treats an empty value as absent
and returns the coded default (L31), which is safe here only because absent,
empty and "maker nowhere" all coincide. A non-empty default could not be turned
off by clearing the repository variable, and a test pins this.
"""
from __future__ import annotations

import logging
from typing import List

from src.trading_config import (
    MAKER_ENABLED,
    MAKER_ENABLED_SERIES,
    PAPER_CONSERVATIVE_FILLS,
)

logger = logging.getLogger(__name__)

TAKER = "taker"
MAKER = "maker"


def enabled_series() -> List[str]:
    return [s.strip().upper() for s in MAKER_ENABLED_SERIES.split(",") if s.strip()]


def _series_of(market_id: str) -> str:
    return (market_id or "").split("-", 1)[0].upper()


def maker_allowed_for(market_id: str) -> bool:
    """True only if the master is on AND this series is on the list."""
    if not MAKER_ENABLED:
        return False
    series = _series_of(market_id)
    return bool(series) and series in set(enabled_series())


def order_type_for(market_id: str) -> str:
    """The order type for this market, resolved ONCE.

    Evaluation and execution must both use the value this returns, threaded
    from a single call. Reading it independently in two places is how trade
    1/50 was evaluated at 91c and filled at 92c — either side of the 0.03 gate
    it passed on. `src/ev/fills.py` exists because of that.
    """
    return MAKER if maker_allowed_for(market_id) else TAKER


def describe() -> str:
    """One line of state, printed every cycle INCLUDING when nothing is on.

    A control that reports only when it is doing something makes "off" and
    "did nothing this time" identical, which cost a day on 2026-08-17 (L31).

    The two enabled states are named separately and deliberately: a series on
    the allow-list still does not change paper fill pricing while
    PAPER_CONSERVATIVE_FILLS is on, so "enabled" means shadow-only until that
    flag is separately and deliberately changed. Two config states that mean
    different things must never render identically.
    """
    if not MAKER_ENABLED:
        return "maker: OFF (global master disabled)"

    series = enabled_series()
    if not series:
        return "maker: OFF (master on, no series enabled)"

    listed = ", ".join(sorted(series))
    if PAPER_CONSERVATIVE_FILLS:
        return (
            f"maker: ENABLED (SHADOW-ONLY) for {listed} — paper fills still "
            f"priced at the touch while PAPER_CONSERVATIVE_FILLS is on"
        )
    return f"maker: ENABLED (PRICING PAPER FILLS) for {listed}"


# The preflight verdict for this process. One cycle is one process, so caching
# it here evaluates the checklist exactly once per cycle no matter how many
# markets are resolved — the ruling is CONTINUOUS, not per-market.
_BLOCKERS = None


def _reset_blocker_cache() -> None:
    global _BLOCKERS
    _BLOCKERS = None


def _run_checklist(engine):
    from src.execution.preflight import maker_blockers

    return maker_blockers(engine)


def _maker_blockers(engine) -> List[str]:
    global _BLOCKERS
    if _BLOCKERS is None:
        try:
            _BLOCKERS = list(_run_checklist(engine))
        except Exception as exc:
            # Unknown is not clear. A checklist that cannot be evaluated is a
            # blocker in its own right.
            logger.error("Maker preflight could not be evaluated: %s", exc)
            _BLOCKERS = [f"checklist error: {exc}"]
    return _BLOCKERS


def resolve_order_type(engine, market_id: str) -> str:
    """The authoritative order type: allow-list AND a currently-clean checklist.

    The checklist is re-evaluated every cycle while any series is enabled, not
    once when the first series was added. A gate that runs once is a gate that
    was true once — the decimal migration, the capture-beats-taker check and
    the live-gate check can each stop being true afterwards, and a
    first-addition-only gate would never look again.

    A blocker forces TAKER for every enabled series, loudly. It does not abort
    the trade: taker is the conservative execution style and the one maker was
    being promoted away from, so degrading to it is the fail-closed direction.
    Refusing to trade would turn a maker misconfiguration into a full paper
    trading outage, which is strictly worse.
    """
    if order_type_for(market_id) != MAKER:
        # Nothing to gate, and no DB work while maker is off everywhere.
        return TAKER

    blockers = _maker_blockers(engine)
    if blockers:
        logger.error(
            "MAKER FORCED OFF for every enabled series — preflight checklist "
            "is not satisfied:\n  - %s",
            "\n  - ".join(blockers),
        )
        return TAKER
    return MAKER
