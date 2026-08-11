"""Run a maker order in shadow: record what it would have done, trade nothing.

Writes only to `shadow_maker_orders`. The real taker paper fill happens exactly
as before, untouched, so the 50-trade gate keeps accruing on the validated
execution path and the shadow record cannot contaminate it.

Reporting keeps two distinct floors apart and NEVER pools them:

  CAPTURE    per-fill economics — taker price minus maker fill price, on the
             fills the rule recognises. A floor on what maker is worth WHEN it
             fills.
  FREQUENCY  how often it filled at all. A floor on volume.

They are separate because the fill rule distorts them in different ways.
Frequency is understated by construction (only ~10% of taker events trade
through a level). Capture is measured on a set that over-represents adverse
selection, since a trade-through means the market moved decisively against our
resting side. Multiplying one by the other and calling it maker PnL would
launder both biases into a single number that looks like a verdict and is not.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.execution.fill_sim import has_gap, load_tape, simulate_rest
from src.execution.walkup import build_plan
from src.models.shadow import ShadowMakerOrder
from src.trading.fees import kalshi_fee

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass
class ShadowOutcome:
    status: str
    filled: Decimal
    final_price_cents: Optional[int]
    steps: int
    capture_cents: Optional[Decimal]
    reason: str


def simulate_order(
    engine: Engine,
    market_id: str,
    side: str,
    quantity: Decimal,
    start_price_cents: int,
    taker_price_cents: int,
    p_model: float,
    required_edge: float,
    rest_start_ms: int,
    rest_seconds: float = 30.0,
    category: str = "",
    model_name: str = "",
) -> ShadowOutcome:
    """Walk a maker order through the recorded tape and persist the outcome."""
    plan = build_plan(start_price_cents, p_model, required_edge, side)

    if not plan.steps:
        outcome = ShadowOutcome("not_placed", ZERO, None, 0, None, plan.reason)
        _persist(engine, locals(), plan, outcome)
        return outcome

    window_ms = int(rest_seconds * 1000)
    tape = load_tape(
        engine, market_id, rest_start_ms,
        rest_start_ms + window_ms * len(plan.steps),
    )

    filled = ZERO
    remaining = quantity
    last_price = None
    steps_taken = 0
    weighted_cost = ZERO
    unproven = False
    reasons: List[str] = []

    for step in plan.steps:
        steps_taken += 1
        last_price = step.price_cents
        start = rest_start_ms + window_ms * step.index
        end = start + window_ms
        gap = has_gap(engine, market_id, start, end)

        result = simulate_rest(
            tape, side, step.price_cents, remaining, start, end, gap_present=gap,
        )
        if result.unproven:
            unproven = True
            reasons.append(f"step {step.index}: {result.reason}")
            break
        if result.any_fill:
            filled += result.filled
            weighted_cost += result.filled * Decimal(step.price_cents)
            remaining -= result.filled
        reasons.append(f"step {step.index} @ {step.price_cents}c: {result.reason}")
        if remaining <= ZERO:
            break

    if unproven:
        status = "unproven"
        capture = None
    elif filled <= ZERO:
        status = "unfilled"
        capture = None
    else:
        status = "filled" if remaining <= ZERO else "partial"
        average = weighted_cost / filled
        # Positive means the maker path paid LESS than crossing the spread.
        capture = Decimal(taker_price_cents) - average

    outcome = ShadowOutcome(status, filled, last_price, steps_taken, capture,
                            "; ".join(reasons) or plan.reason)
    _persist(engine, locals(), plan, outcome)
    return outcome


def _persist(engine: Engine, scope: dict, plan, outcome: ShadowOutcome) -> None:
    filled = outcome.filled
    price = outcome.final_price_cents or scope["start_price_cents"]
    with get_session(engine) as session:
        session.add(ShadowMakerOrder(
            market_id=scope["market_id"],
            category=scope.get("category", "") or "",
            model_name=scope.get("model_name") or None,
            side=scope["side"],
            intended_quantity=scope["quantity"],
            filled_quantity=filled,
            start_price_cents=scope["start_price_cents"],
            final_price_cents=outcome.final_price_cents,
            steps_taken=outcome.steps,
            cap_cents=plan.cap_cents,
            capped=plan.capped_early,
            taker_price_cents=scope["taker_price_cents"],
            capture_cents=outcome.capture_cents,
            maker_fee=Decimal(str(kalshi_fee(int(filled), price))) if filled > ZERO else None,
            taker_fee=(
                Decimal(str(kalshi_fee(int(filled), scope["taker_price_cents"])))
                if filled > ZERO else None
            ),
            status=outcome.status,
            reason=outcome.reason[:2000],
            rest_start_ms=scope["rest_start_ms"],
        ))
        session.commit()


# --------------------------------------------------------------------------
# reporting — two floors, never one number
# --------------------------------------------------------------------------

@dataclass
class CategoryReport:
    category: str
    orders: int = 0
    recognised_fills: int = 0
    unproven: int = 0
    not_placed: int = 0
    mean_capture_cents: Optional[float] = None
    total_filled: Decimal = ZERO

    @property
    def fill_frequency(self) -> Optional[float]:
        """Share of placed orders the rule recognised as filling.

        A FLOOR on volume, never an estimate of it: only ~10% of taker events
        trade through a level, so real maker orders fill more often than this.
        """
        placed = self.orders - self.not_placed - self.unproven
        return (self.recognised_fills / placed) if placed else None


def report_by_category(engine: Engine) -> List[CategoryReport]:
    """Per-category shadow results. Deliberately not pooled across categories.

    A liquid category must not carry an illiquid one through validation: sports
    parlays and daily weather contracts have different spreads, different depth,
    and different trade-through rates, so one aggregate number would let the
    first vouch for the second.
    """
    with get_session(engine) as session:
        rows = session.execute(
            select(
                ShadowMakerOrder.category,
                ShadowMakerOrder.status,
                func.count(),
                func.avg(ShadowMakerOrder.capture_cents),
                func.sum(ShadowMakerOrder.filled_quantity),
            ).group_by(ShadowMakerOrder.category, ShadowMakerOrder.status)
        ).all()

    reports: Dict[str, CategoryReport] = {}
    captures: Dict[str, List[float]] = {}
    for category, status, count, avg_capture, total_filled in rows:
        report = reports.setdefault(category or "unknown", CategoryReport(category or "unknown"))
        report.orders += count
        if status in ("filled", "partial"):
            report.recognised_fills += count
            report.total_filled += Decimal(str(total_filled or 0))
            if avg_capture is not None:
                captures.setdefault(report.category, []).append(float(avg_capture))
        elif status == "unproven":
            report.unproven += count
        elif status == "not_placed":
            report.not_placed += count

    for category, values in captures.items():
        reports[category].mean_capture_cents = sum(values) / len(values)

    return sorted(reports.values(), key=lambda r: r.category)


def format_report(reports: List[CategoryReport]) -> str:
    """Render both floors side by side, labelled as floors."""
    if not reports:
        return "shadow maker: no simulated orders yet"

    lines = ["shadow maker (both figures are FLOORS, not estimates):"]
    for report in reports:
        frequency = report.fill_frequency
        frequency_text = f"{frequency:.1%}" if frequency is not None else "n/a"
        capture_text = (
            f"{report.mean_capture_cents:+.2f}c/contract"
            if report.mean_capture_cents is not None else "n/a"
        )
        lines.append(
            f"  {report.category}: capture {capture_text} on "
            f"{report.recognised_fills} recognised fills | "
            f"fill frequency >= {frequency_text} of {report.orders} orders"
        )
        if report.unproven:
            lines.append(f"    {report.unproven} unproven (sequence gaps)")
        if report.not_placed:
            lines.append(f"    {report.not_placed} not placed (cap below start price)")
    lines.append(
        "  capture is measured on trade-throughs, which over-represent adverse "
        "selection; frequency is understated by construction. Do not multiply them."
    )
    return "\n".join(lines)
