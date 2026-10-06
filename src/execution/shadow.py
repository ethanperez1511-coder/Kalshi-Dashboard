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
from src.execution.fill_sim import coverage, has_gap, load_tape, simulate_rest
from src.execution.walkup import build_plan
from src.models.shadow import ShadowMakerOrder
from src.trading.fees import kalshi_fee
from src.trading_config import MAKER_REST_SECONDS, MAKER_STEP_CENTS

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


def passive_start_cents(side: str, yes_bid: int, yes_ask: int, taker_price_cents: int) -> int:
    """Where a maker order on `side` starts: one cent inside our side's best
    bid, or joining the bid when the spread is a single cent. Never at or
    above the price a taker pays. (Until 2026-10-06 it started AT the taker
    price and walked up, which is not a maker order at all.)"""
    bid = yes_bid if side == "yes" else 100 - yes_ask
    start = bid + 1 if bid >= 1 else 1
    if start >= taker_price_cents:
        start = bid
    return max(1, min(start, taker_price_cents - 1))


def _walk(
    engine: Engine, market_id: str, side: str, quantity: Decimal, prices: List[int],
    rest_start_ms: int, rest_seconds: float, taker_price_cents: int,
) -> ShadowOutcome:
    """Rest at each price in turn against the recorded tape."""
    window_ms = int(rest_seconds * 1000)
    tape = load_tape(engine, market_id, rest_start_ms, rest_start_ms + window_ms * len(prices))

    filled = ZERO
    remaining = quantity
    last_price = None
    steps_taken = 0
    weighted_cost = ZERO
    reasons: List[str] = []

    for index, price in enumerate(prices):
        steps_taken += 1
        last_price = price
        start = rest_start_ms + window_ms * index
        end = start + window_ms
        result = simulate_rest(
            tape, side, price, remaining, start, end,
            gap_present=has_gap(engine, market_id, start, end),
        )
        if result.unproven:
            return ShadowOutcome("unproven", ZERO, price, steps_taken, None,
                                 f"step {index}: {result.reason}")
        if result.any_fill:
            filled += result.filled
            weighted_cost += result.filled * Decimal(price)
            remaining -= result.filled
        reasons.append(f"step {index} @ {price}c: {result.reason}")
        if remaining <= ZERO:
            break

    if filled <= ZERO:
        return ShadowOutcome("unfilled", ZERO, last_price, steps_taken, None, "; ".join(reasons))
    average = weighted_cost / filled
    # Positive means the maker path paid LESS than crossing the spread.
    capture = Decimal(taker_price_cents) - average
    status = "filled" if remaining <= ZERO else "partial"
    return ShadowOutcome(status, filled, last_price, steps_taken, capture, "; ".join(reasons))


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
    maker_rate: float = 0.0,
    taker_rate: Optional[float] = None,
) -> ShadowOutcome:
    """Judge a maker order against the tape NOW and persist the outcome.

    Only correct when the rest window has already closed and its tape is
    recorded, which is why production does NOT call it: production places
    with `place_order` and judges later with `resolve_pending`. Kept for the
    walk's unit tests, where the tape is laid down first.
    """
    plan = build_plan(start_price_cents, p_model, required_edge, side,
                      ceiling_cents=taker_price_cents - 1)
    if not plan.steps:
        outcome = ShadowOutcome("not_placed", ZERO, None, 0, None, plan.reason)
    else:
        outcome = _walk(engine, market_id, side, quantity,
                        [st.price_cents for st in plan.steps],
                        rest_start_ms, rest_seconds, taker_price_cents)
    _persist(engine, locals(), plan, outcome)
    return outcome


def place_order(
    engine: Engine,
    market_id: str,
    side: str,
    quantity: Decimal,
    yes_bid: int,
    yes_ask: int,
    taker_price_cents: int,
    p_model: float,
    required_edge: float,
    rest_start_ms: int,
    maker_rate: float,
    taker_rate: float,
    rest_seconds: float = MAKER_REST_SECONDS,
    category: str = "",
    model_name: str = "",
) -> str:
    """Record a maker order as PENDING. Nothing is judged here: the window it
    will rest over is in the future, so its tape cannot exist yet. Returns the
    status written ("pending", or "not_placed" when no price clears the cap)."""
    start_price_cents = passive_start_cents(side, yes_bid, yes_ask, taker_price_cents)
    plan = build_plan(start_price_cents, p_model, required_edge, side,
                      ceiling_cents=taker_price_cents - 1, rest_seconds=rest_seconds)
    if not plan.steps:
        outcome = ShadowOutcome("not_placed", ZERO, None, 0, None, plan.reason)
    else:
        outcome = ShadowOutcome("pending", ZERO, None, 0, None,
                                f"resting {len(plan.steps)} step(s) from {start_price_cents}c")
    planned_steps = len(plan.steps)
    _persist(engine, locals(), plan, outcome)
    return outcome.status


# Recorder rows reach the database in batches; give them time before judging.
RESOLVE_GRACE = dt.timedelta(minutes=5)
# A window with no provable coverage by then never will have it.
UNPROVEN_AFTER = dt.timedelta(hours=2)


def resolve_pending(engine: Engine, now: Optional[dt.datetime] = None) -> Dict[str, int]:
    """Judge every pending order whose window has closed. Called each cycle.

    Covered and gap-free: walk the tape. A gap from the window's start until
    the connection's next row: unproven. Not (yet) covered: stay pending,
    until UNPROVEN_AFTER, then unproven. Never `unfilled` for want of tape.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    counts: Dict[str, int] = {}
    with get_session(engine) as session:
        pending = session.execute(
            select(ShadowMakerOrder).where(ShadowMakerOrder.status == "pending")
        ).scalars().all()
        for row in pending:
            window_end_ms = row.rest_start_ms + int(row.rest_seconds * 1000) * row.planned_steps
            window_end = dt.datetime.fromtimestamp(window_end_ms / 1000, dt.timezone.utc)
            if now < window_end + RESOLVE_GRACE:
                continue

            covered, why = coverage(engine, row.market_id, row.rest_start_ms, window_end_ms)
            if covered and has_gap(engine, row.market_id, row.rest_start_ms, window_end_ms):
                outcome = ShadowOutcome("unproven", ZERO, None, 0, None,
                                        "sequence gap on the subscription during or just after the window")
            elif covered:
                prices = [row.start_price_cents + i * MAKER_STEP_CENTS
                          for i in range(row.planned_steps)]
                outcome = _walk(engine, row.market_id, row.side, Decimal(row.intended_quantity),
                                prices, row.rest_start_ms, row.rest_seconds, row.taker_price_cents)
            elif now >= window_end + UNPROVEN_AFTER:
                outcome = ShadowOutcome("unproven", ZERO, None, 0, None, f"no recorder coverage: {why}")
            else:
                continue

            _apply_outcome(row, outcome)
            row.resolved_at = now
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        session.commit()
    return counts


def _fees(row_or_scope, filled: Decimal, price: Optional[int]) -> tuple:
    """(maker_fee, taker_fee) in dollars at the series' own schedule. The maker
    leg pays the MAKER rate, zero on standard series; until 2026-10-06 it was
    charged the taker fee, understating capture on every row."""
    if filled <= ZERO or price is None:
        return None, None
    get = (lambda k: row_or_scope.get(k)) if isinstance(row_or_scope, dict) else (
        lambda k: getattr(row_or_scope, k))
    maker_rate = get("maker_rate") or 0.0
    taker_rate = get("taker_rate")
    taker_price = get("taker_price_cents")
    maker = Decimal(str(kalshi_fee(int(filled), price, maker_rate))) if maker_rate else Decimal("0")
    taker = Decimal(str(
        kalshi_fee(int(filled), taker_price, taker_rate) if taker_rate is not None
        else kalshi_fee(int(filled), taker_price)
    ))
    return maker, taker


def _apply_outcome(row: ShadowMakerOrder, outcome: ShadowOutcome) -> None:
    row.status = outcome.status
    row.filled_quantity = outcome.filled
    row.final_price_cents = outcome.final_price_cents
    row.steps_taken = outcome.steps
    row.capture_cents = outcome.capture_cents
    row.maker_fee, row.taker_fee = _fees(row, outcome.filled, outcome.final_price_cents)
    row.reason = (outcome.reason or "")[:2000]


def _persist(engine: Engine, scope: dict, plan, outcome: ShadowOutcome) -> None:
    filled = outcome.filled
    price = outcome.final_price_cents or scope["start_price_cents"]
    maker_fee, taker_fee = _fees(scope, filled, price if filled > ZERO else None)
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
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            status=outcome.status,
            reason=outcome.reason[:2000],
            rest_start_ms=scope["rest_start_ms"],
            planned_steps=scope.get("planned_steps"),
            rest_seconds=scope.get("rest_seconds"),
            maker_rate=scope.get("maker_rate"),
            taker_rate=scope.get("taker_rate"),
            resolved_at=None if outcome.status == "pending" else dt.datetime.now(dt.timezone.utc),
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
    pending: int = 0
    mean_capture_cents: Optional[float] = None
    total_filled: Decimal = ZERO

    @property
    def fill_frequency(self) -> Optional[float]:
        """Share of placed orders the rule recognised as filling.

        A FLOOR on volume, never an estimate of it: only ~10% of taker events
        trade through a level, so real maker orders fill more often than this.
        """
        placed = self.orders - self.not_placed - self.unproven - self.pending
        return (self.recognised_fills / placed) if placed else None


def report_by_category(engine: Engine) -> List[CategoryReport]:
    """Per-category shadow results. Deliberately not pooled across categories.

    A liquid category must not carry an illiquid one through validation: sports
    parlays and daily weather contracts have different spreads, different depth,
    and different trade-through rates, so one aggregate number would let the
    first vouch for the second.
    """
    return _report(engine, lambda market_id, category: category or "unknown")


def report_by_series(engine: Engine) -> List[CategoryReport]:
    """Per-SERIES results, the unit the maker allow-list enables. Never pooled:
    KXHIGHNY's fills say nothing about KXHIGHTSEA's book."""
    from src.ingestion.exclusions import series_of

    return _report(engine, lambda market_id, category: series_of(market_id) or "unknown")


def _report(engine: Engine, key) -> List[CategoryReport]:
    with get_session(engine) as session:
        # Aggregated by the server: one row per (market, category, status),
        # never one per order.
        rows = session.execute(
            select(
                ShadowMakerOrder.market_id,
                ShadowMakerOrder.category,
                ShadowMakerOrder.status,
                func.count(),
                func.sum(ShadowMakerOrder.capture_cents),
                func.count(ShadowMakerOrder.capture_cents),
                func.sum(ShadowMakerOrder.filled_quantity),
            ).group_by(
                ShadowMakerOrder.market_id, ShadowMakerOrder.category, ShadowMakerOrder.status,
            )
        ).all()

    reports: Dict[str, CategoryReport] = {}
    capture_sum: Dict[str, float] = {}
    capture_n: Dict[str, int] = {}
    for market_id, category, status, count, cap_sum, cap_n, filled in rows:
        name = key(market_id, category)
        report = reports.setdefault(name, CategoryReport(name))
        report.orders += count
        if status in ("filled", "partial"):
            report.recognised_fills += count
            report.total_filled += Decimal(str(filled or 0))
            if cap_n:
                capture_sum[name] = capture_sum.get(name, 0.0) + float(cap_sum)
                capture_n[name] = capture_n.get(name, 0) + cap_n
        elif status == "unproven":
            report.unproven += count
        elif status == "not_placed":
            report.not_placed += count
        elif status == "pending":
            report.pending += count

    # Mean over FILLS, not a mean of per-group means.
    for name, n in capture_n.items():
        reports[name].mean_capture_cents = capture_sum[name] / n

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
        if report.pending:
            lines.append(f"    {report.pending} pending (window not yet judged)")
        if report.unproven:
            lines.append(f"    {report.unproven} unproven (gap or no recorder coverage)")
        if report.not_placed:
            lines.append(f"    {report.not_placed} not placed (cap below start price)")
    lines.append(
        "  capture is measured on trade-throughs, which over-represent adverse "
        "selection; frequency is understated by construction. Do not multiply them."
    )
    return "\n".join(lines)
