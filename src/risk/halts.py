"""Kill switches: latched halts that only a human clears (ruling 2026-10-09).

The limits in limits.py are per-trade refusals recomputed every cycle, so the
drawdown breaker un-trips the moment equity bounces. A halt here is LATCHED: a
row in halt_events that stays active until a human runs
`python -m src.maintenance --clear-halt --confirm CLEAR-HALT`.

A halt refuses NEW trades and nothing else. It never places or closes an
order, never touches a limit, Kelly, `mode` or the live gate, and settlement
keeps running (the settler runs before scoring every cycle).

The thresholds are hardcoded and pinned by a test, like the risk limits:
loosening one is a decision, not an edit.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, List, Optional

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.cycle_run import CycleRun
from src.models.halt import HaltEvent

logger = logging.getLogger(__name__)

DRAWDOWN_LATCH_PCT = 0.20        # equity this far below its peak
BANKROLL_DROP_PCT = 0.10         # equity this far below its 24 h high
IMPLAUSIBLE_EDGE_POINTS = 40     # |model - price| on the traded side, in cents
SLIPPAGE_CENTS = 3               # |fill - evaluated price|
REPEATED_ERROR_CYCLES = 3        # this many failed cycles in a row
STALE_INPUT_MINUTES = 60         # newest price snapshot older than this


def _utc(t: Optional[dt.datetime]) -> Optional[dt.datetime]:
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _send_alert(message: str) -> None:
    from src.alerts import Alerter

    if not Alerter().send(message):
        logger.error("Halt alert NOT delivered: %s", message)


def active_halts(engine: Engine) -> List[HaltEvent]:
    with get_session(engine) as session:
        rows = session.execute(
            select(HaltEvent).where(HaltEvent.cleared_at.is_(None)).order_by(HaltEvent.tripped_at)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def trip(engine: Engine, switch: str, detail: str,
         alert: Optional[Callable[[str], object]] = None,
         now: Optional[dt.datetime] = None) -> bool:
    """Latch a halt. Idempotent per switch: a second trip of an active switch
    records nothing and alerts nothing. Returns True if newly tripped."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with get_session(engine) as session:
        exists = session.execute(
            select(HaltEvent.id).where(HaltEvent.switch == switch, HaltEvent.cleared_at.is_(None))
        ).first()
        if exists:
            return False
        session.add(HaltEvent(switch=switch, detail=detail[:2000], tripped_at=now))
        session.commit()
    logger.error("HALT %s: %s", switch, detail)
    (alert or _send_alert)(
        f"🛑 <b>TRADING HALTED</b> ({switch}): {detail}\n"
        f"No new trades until cleared by hand: "
        f"python -m src.maintenance --clear-halt --confirm CLEAR-HALT"
    )
    return True


def clear_halts(engine: Engine, by: str, now: Optional[dt.datetime] = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    with get_session(engine) as session:
        rows = session.execute(
            select(HaltEvent).where(HaltEvent.cleared_at.is_(None))
        ).scalars().all()
        for row in rows:
            row.cleared_at, row.cleared_by = now, by[:60]
        session.commit()
        return len(rows)


# --------------------------------------------------------------------------
# Per-cycle switches
# --------------------------------------------------------------------------

def consecutive_failures(engine: Engine) -> int:
    with get_session(engine) as session:
        oks = session.execute(
            select(CycleRun.ok).order_by(CycleRun.started_at.desc()).limit(REPEATED_ERROR_CYCLES)
        ).scalars().all()
    count = 0
    for ok in oks:
        if ok:
            break
        count += 1
    return count


def check_repeated_errors(engine: Engine, alert=None, now=None) -> bool:
    failures = consecutive_failures(engine)
    if failures >= REPEATED_ERROR_CYCLES:
        return trip(engine, "repeated_errors",
                    f"the last {failures} cycles all failed", alert=alert, now=now)
    return False


def check_cycle_halts(engine: Engine, alert: Callable[[str], object],
                      now: Optional[dt.datetime] = None) -> List[str]:
    """Run before execution each cycle. Returns the switches newly tripped."""
    from src.models.price import PriceSnapshot
    from src.models.settings import TradingSettings
    from src.portfolio.equity import total_equity

    now = now or dt.datetime.now(dt.timezone.utc)
    tripped: List[str] = []

    equity = total_equity(engine)
    settings = TradingSettings.get_or_create(engine)
    peak = settings.peak_bankroll or 0.0
    if peak > 0 and (peak - equity) / peak >= DRAWDOWN_LATCH_PCT:
        if trip(engine, "drawdown_latch",
                f"equity ${equity:.2f} is {(peak - equity) / peak:.0%} below peak ${peak:.2f}",
                alert=alert, now=now):
            tripped.append("drawdown_latch")

    with get_session(engine) as session:
        high = session.execute(
            select(func.max(CycleRun.equity)).where(CycleRun.started_at >= now - dt.timedelta(hours=24))
        ).scalar()
        newest = _utc(session.execute(select(func.max(PriceSnapshot.timestamp))).scalar())
    if high and (high - equity) / high >= BANKROLL_DROP_PCT:
        if trip(engine, "bankroll_drop",
                f"equity ${equity:.2f} is {(high - equity) / high:.0%} below its 24 h high ${high:.2f}",
                alert=alert, now=now):
            tripped.append("bankroll_drop")

    if newest is None or now - newest > dt.timedelta(minutes=STALE_INPUT_MINUTES):
        age = "none recorded" if newest is None else f"{(now - newest).total_seconds() / 60:.0f} min old"
        if trip(engine, "stale_inputs", f"newest price snapshot is {age}", alert=alert, now=now):
            tripped.append("stale_inputs")
    return tripped


# --------------------------------------------------------------------------
# Per-trade switches
# --------------------------------------------------------------------------

def _side_price(opp: dict) -> Optional[int]:
    price = opp.get("evaluated_price")
    if price:
        return int(price)
    if opp.get("recommended_side") == "no":
        bid = opp.get("yes_bid")
        return 100 - int(bid) if bid else None
    ask = opp.get("yes_ask")
    return int(ask) if ask else None


def implausible_edge(opp: dict) -> Optional[str]:
    """A model this far from the market is far likelier broken than right."""
    price = _side_price(opp)
    if price is None:
        return None
    p_side = opp["p_model"] if opp.get("recommended_side", "yes") == "yes" else 1 - opp["p_model"]
    gap = abs(p_side * 100 - price)
    if gap >= IMPLAUSIBLE_EDGE_POINTS:
        return (f"{opp['market_id']}: model {p_side:.0%} vs price {price}c on the "
                f"{opp.get('recommended_side', 'yes').upper()} side ({gap:.0f} points)")
    return None


def fill_slippage(evaluated: Optional[int], filled: Optional[int]) -> Optional[str]:
    if evaluated is None or filled is None:
        return None
    if abs(int(filled) - int(evaluated)) >= SLIPPAGE_CENTS:
        return f"filled at {filled}c against an evaluated {evaluated}c"
    return None


def format_halts(engine: Engine) -> str:
    rows = active_halts(engine)
    if not rows:
        return "🟢 Halts: none"
    return "\n".join(
        f"🛑 HALTED ({h.switch}) since {_utc(h.tripped_at):%Y-%m-%d %H:%MZ}: {h.detail} "
        f"— clear: python -m src.maintenance --clear-halt --confirm CLEAR-HALT"
        for h in rows
    )
