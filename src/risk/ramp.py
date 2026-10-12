"""Live size ramp (approved 2026-10-12; design in docs/size-ramp.md).

    quantity = min(risk-approved quantity, stage cap)      LIVE mode only

Applied at the very end of RiskManager.evaluate, after quarter-Kelly and every
hard limit have approved. It can only LOWER a trade's size: it never raises a
cap, never touches a limit, and never reads or writes `mode` except to check
whether it applies. Paper sizing is untouched.

Promotion is human (`--promote-ramp --confirm PROMOTE-RAMP`), refused unless
every evidence bar holds. Demotion is automatic: any kill-switch trip returns
the ramp to stage 0.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional, Tuple

from sqlalchemy import Engine, select

from src.database import get_session
from src.models.halt import HaltEvent
from src.models.ramp_event import RampEvent
from src.models.settings import TradingSettings
from src.models.trade import Trade

STAGE_CAPS = {0: 1, 1: 3, 2: 10, 3: None}        # max contracts per live trade
STAGE_MIN_TRADES = {0: 20, 1: 20, 2: 40}          # live trades before promotion
FINAL_STAGE = 3
MAX_MEAN_SLIPPAGE_CENTS = 1.0
MAX_SLIPPAGE_CENTS = 2


def _utc(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def ramped_quantity(risk_quantity: int, stage: int) -> int:
    cap = STAGE_CAPS[stage]
    return risk_quantity if cap is None else min(risk_quantity, cap)


def current_stage(engine: Engine) -> int:
    with get_session(engine) as session:
        row = session.query(TradingSettings).first()
        return (row.ramp_stage or 0) if row else 0


def _set_stage(engine: Engine, stage: int, reason: str, by: str,
               now: Optional[dt.datetime] = None) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    with get_session(engine) as session:
        row = session.query(TradingSettings).first()
        before = (row.ramp_stage or 0) if row else 0
        if row is None or before == stage:
            return
        row.ramp_stage, row.ramp_stage_started_at = stage, now
        session.add(RampEvent(at=now, from_stage=before, to_stage=stage,
                              reason=reason[:2000], by=by[:60]))
        session.commit()


def demote_to_zero(engine: Engine, reason: str, now: Optional[dt.datetime] = None) -> None:
    """Called on every kill-switch trip. A cleared halt resumes at 1 contract."""
    _set_stage(engine, 0, f"kill switch: {reason}", by="automatic", now=now)


# --------------------------------------------------------------------------
# Evidence bars: each returns (name, passed, detail)
# --------------------------------------------------------------------------

def _stage_trades(engine: Engine, since: Optional[dt.datetime]):
    with get_session(engine) as session:
        q = select(Trade).where(Trade.is_paper.is_(False))
        if since is not None:
            q = q.where(Trade.created_at >= since)
        rows = session.execute(q).scalars().all()
        for r in rows:
            session.expunge(r)
        return rows


def eligibility(engine: Engine, now: Optional[dt.datetime] = None) -> Tuple[bool, List[tuple]]:
    from src.ingestion.exclusions import series_of
    from src.models.fees import SeriesFee
    from src.trading.fee_schedule import BASE_TAKER_RATE
    from src.trading.fees import kalshi_fee

    stage = current_stage(engine)
    if stage >= FINAL_STAGE:
        return False, [("final_stage", False, "already at the final stage")]
    with get_session(engine) as session:
        row = session.query(TradingSettings).first()
        since = _utc(row.ramp_stage_started_at) if row else None
        rates = {f.series: f.fee_multiplier for f in session.execute(select(SeriesFee)).scalars()}
        halts_in_stage = session.execute(
            select(HaltEvent.id).where(HaltEvent.tripped_at >= since) if since
            else select(HaltEvent.id)
        ).first()
    trades = _stage_trades(engine, since)
    bars: List[tuple] = []

    need = STAGE_MIN_TRADES[stage]
    bars.append(("trade_count", len(trades) >= need, f"{len(trades)}/{need} live trades in stage {stage}"))

    slips = [abs(t.price - t.evaluated_price) for t in trades if t.evaluated_price is not None]
    if slips:
        mean, worst = sum(slips) / len(slips), max(slips)
        ok = mean <= MAX_MEAN_SLIPPAGE_CENTS and worst <= MAX_SLIPPAGE_CENTS
        bars.append(("slippage", ok, f"mean {mean:.2f}c, worst {worst}c (bar ≤{MAX_MEAN_SLIPPAGE_CENTS}c / ≤{MAX_SLIPPAGE_CENTS}c)"))
    else:
        bars.append(("slippage", False, "no live fills with an evaluated price"))

    mismatched = [
        t.market_id for t in trades
        if t.entry_fee is not None and abs(
            t.entry_fee - kalshi_fee(int(t.quantity), t.price,
                                     BASE_TAKER_RATE * rates.get(series_of(t.market_id), 1.0))
        ) > 0.005
    ]
    bars.append(("fees_match", bool(trades) and not mismatched,
                 "no live trades" if not trades else
                 (f"{len(mismatched)} fills charged off-schedule" if mismatched else "every fill on schedule")))

    bars.append(("no_halts", halts_in_stage is None,
                 "a kill switch tripped during this stage" if halts_in_stage else "none during this stage"))

    settled = [t for t in trades if t.status == "closed" and t.realized_pnl is not None]
    if settled:
        p = sum((t.p_model if t.side == "yes" else 1 - t.p_model) for t in settled) / len(settled)
        wr = sum(1 for t in settled if t.realized_pnl > 0) / len(settled)
        se = math.sqrt(max(p * (1 - p), 1e-9) / len(settled))
        bars.append(("calibration", abs(wr - p) <= 2 * se,
                     f"win rate {wr:.0%} vs model {p:.0%} (±{2 * se:.0%}, n={len(settled)})"))
    else:
        bars.append(("calibration", False, "no settled live trades"))

    # No Kalshi position/ledger reconciler exists yet. Unmeasured is not met:
    # promotion stays impossible until one is built.
    bars.append(("ledger_parity", False,
                 "unmeasured: no reconciler against Kalshi /portfolio yet — build one before any promotion"))

    return all(b[1] for b in bars), bars


def promote(engine: Engine, by: str, now: Optional[dt.datetime] = None) -> bool:
    ok, _ = eligibility(engine, now=now)
    if not ok:
        return False
    stage = current_stage(engine)
    _set_stage(engine, stage + 1, "evidence bars met; promoted by hand", by=by, now=now)
    return True


def format_ramp(engine: Engine) -> str:
    with get_session(engine) as session:
        row = session.query(TradingSettings).first()
        mode = row.mode if row else "paper"
    stage = current_stage(engine)
    cap = STAGE_CAPS[stage]
    head = f"📶 Ramp: stage {stage} (cap {cap or 'none'} contract{'s' if cap != 1 else ''})"
    if mode != "live":
        return head + " — inactive in paper mode"
    ok, bars = eligibility(engine)
    unmet = [b[0] for b in bars if not b[1]]
    return head + (" — eligible for promotion" if ok else f" — not yet: {', '.join(unmet)}")
