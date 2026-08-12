"""Reconcile the production trade record, and unwind positions the current
gates would refuse to have opened.

Runs from Actions because Neon is only reachable there. Two phases, always in
this order: report what is actually in the database, then act on it — and never
act without an explicit confirmation token. A maintenance job that closes
positions on a schedule is a trading strategy nobody approved.

Model attribution on old rows is INFERRED, not recorded: `model_name` was added
in Phase 2.0, so every trade placed before it has NULL there. The inference
reads the reasoning text the model wrote at the time, and every report marks
which attributions were recorded and which were inferred. An inference that
cannot be made is reported as unknown rather than guessed into a bucket.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.market import Market
from src.models.position import Position
from src.models.price import PriceSnapshot
from src.models.trade import Trade
from src.portfolio.tracker import PortfolioTracker

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "CLOSE-LEGACY-POSITIONS"

# Models whose entries the current gates would refuse. Polymarket-sourced
# entries are the concern: the horizon mismatch understates YES, so a NO entry
# holds the biased side of a bias that will not mean-revert.
UNWIND_MODELS = {"PolymarketModel"}


def infer_model(reasoning: Optional[str], recorded: Optional[str]) -> tuple:
    """(model, source). `source` is 'recorded' or 'inferred' — never conflated."""
    if recorded:
        return recorded, "recorded"
    text = (reasoning or "").lower()
    if "polymarket" in text:
        return "PolymarketModel", "inferred"
    if "parlay" in text or ",ext)" in text:
        return "SportsOddsModel", "inferred"
    if "mos " in text or "held-out bss" in text:
        return "WeatherModel", "inferred"
    return "unknown", "inferred"


@dataclass
class PositionView:
    market_id: str
    side: str
    entry_price: int
    quantity: int
    current_price: Optional[int]
    p_model: Optional[float]
    model: str
    attribution: str
    cost_basis: float
    mark_value: Optional[float]
    unrealised: Optional[float]
    action: str = "hold"
    reason: str = ""


@dataclass
class Reconciliation:
    total_trades: int = 0
    paper_trades: int = 0
    legacy_trades: int = 0
    by_model: Dict[str, int] = field(default_factory=dict)
    attribution_sources: Dict[str, int] = field(default_factory=dict)
    positions: List[PositionView] = field(default_factory=list)
    bankroll: Optional[float] = None

    @property
    def to_close(self) -> List[PositionView]:
        return [p for p in self.positions if p.action == "close"]

    @property
    def to_flag(self) -> List[PositionView]:
        return [p for p in self.positions if p.action == "flag"]


def _current_yes_price(engine: Engine, market_id: str) -> Optional[int]:
    """Latest observed YES price for a market, or None.

    None means no mark: the position is reported and NOT closed. Unwinding at a
    made-up price is worse than leaving it open, because it books a fabricated
    realized PnL that then feeds calibration.
    """
    with get_session(engine) as session:
        row = session.execute(
            select(PriceSnapshot.yes_bid, PriceSnapshot.yes_ask, PriceSnapshot.last_price)
            .where(PriceSnapshot.market_id == market_id)
            .order_by(PriceSnapshot.timestamp.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    yes_bid, yes_ask, last_price = row
    if yes_bid and yes_ask:
        return int((yes_bid + yes_ask) / 2)
    return int(last_price) if last_price else None


def reconcile(engine: Engine) -> Reconciliation:
    """What is actually in this database. No writes."""
    report = Reconciliation()

    with get_session(engine) as session:
        report.total_trades = session.execute(
            select(func.count(Trade.id))
        ).scalar() or 0
        report.paper_trades = session.execute(
            select(func.count(Trade.id)).where(Trade.is_paper.is_(True))
        ).scalar() or 0
        report.legacy_trades = session.execute(
            select(func.count(Trade.id)).where(Trade.is_legacy.is_(True))
        ).scalar() or 0

        trades = session.execute(
            select(Trade.market_id, Trade.model_name, Trade.reasoning, Trade.p_model)
        ).all()

        from src.models.settings import TradingSettings

        settings = session.query(TradingSettings).first()
        # None is not zero: before the first cycle there is no row at all, and
        # reporting that as "$0.00 bankroll" reads as a drained account.
        report.bankroll = settings.bankroll if settings else None

        open_positions = session.execute(
            select(Position.market_id, Position.side, Position.entry_price,
                   Position.quantity, Position.current_price)
            .where(Position.status == "open")
        ).all()

    per_market: Dict[str, tuple] = {}
    for market_id, model_name, reasoning, p_model in trades:
        model, source = infer_model(reasoning, model_name)
        report.by_model[model] = report.by_model.get(model, 0) + 1
        report.attribution_sources[source] = report.attribution_sources.get(source, 0) + 1
        per_market.setdefault(market_id, (model, source, p_model))

    for market_id, side, entry, quantity, current in open_positions:
        model, source, p_model = per_market.get(market_id, ("unknown", "inferred", None))
        mark_yes = _current_yes_price(engine, market_id)
        side_mark = None
        if mark_yes is not None:
            side_mark = mark_yes if side == "yes" else 100 - mark_yes

        cost = entry * quantity / 100.0
        value = (side_mark * quantity / 100.0) if side_mark is not None else None

        view = PositionView(
            market_id=market_id, side=side, entry_price=entry, quantity=quantity,
            current_price=side_mark, p_model=p_model, model=model,
            attribution=source, cost_basis=round(cost, 2),
            mark_value=round(value, 2) if value is not None else None,
            unrealised=round(value - cost, 2) if value is not None else None,
        )

        if model in UNWIND_MODELS:
            if side_mark is None:
                view.action = "flag"
                view.reason = (
                    "Polymarket-sourced but NO current mark — cannot close at a "
                    "real price, and closing at an invented one would book "
                    "fabricated PnL"
                )
            else:
                view.action = "close"
                view.reason = (
                    "sourced from PolymarketModel; the horizon mismatch "
                    "understates YES, so this entry holds the biased side"
                )
        elif model == "unknown":
            view.action = "flag"
            view.reason = "model could not be attributed — review before acting"
        else:
            view.action = "hold"
            view.reason = f"{model} entry — out of scope for this unwind"

        report.positions.append(view)

    return report


def execute_closures(engine: Engine, report: Reconciliation) -> List[dict]:
    """Close the planned positions at their current mark, in paper.

    Uses the same settlement path as a real close, so the resulting trade rows,
    bankroll movement and fee handling are identical to any other close. The
    rows stay legacy, so nothing here feeds the gate or the calibration fit.
    """
    tracker = PortfolioTracker(engine)
    results: List[dict] = []

    for view in report.to_close:
        # close_position takes a YES-scale exit and converts to side terms.
        exit_yes = (
            view.current_price if view.side == "yes" else 100 - view.current_price
        )
        closed = tracker.close_position(view.market_id, exit_yes, finalize_market=False)
        if closed is None:
            results.append({"market_id": view.market_id, "status": "not_found"})
            continue
        results.append({
            "market_id": view.market_id,
            "status": "closed",
            "exit_price": closed["exit_price"],
            "realized_pnl": closed["realized_pnl"],
        })
        logger.info(
            "Unwound %s at %dc — realized $%.2f",
            view.market_id, closed["exit_price"], closed["realized_pnl"],
        )

    # Anything the unwind created or touched is still legacy evidence.
    from src.legacy_cutoff import mark_legacy_trades, resync_gate_counter

    mark_legacy_trades(engine)
    resync_gate_counter(engine)
    return results


def format_report(report: Reconciliation, executed: Optional[List[dict]] = None) -> str:
    lines = [
        "PRODUCTION TRADE RECORD",
        f"  trades: {report.total_trades} total, {report.paper_trades} paper, "
        f"{report.legacy_trades} legacy",
        f"  bankroll: ${report.bankroll:.2f}" if report.bankroll is not None
        else "  bankroll: NO SETTINGS ROW YET (first cycle creates it at $100.00)",
        "  per-model attribution:",
    ]
    for model, count in sorted(report.by_model.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {model:<20} {count}")
    sources = ", ".join(f"{k} {v}" for k, v in sorted(report.attribution_sources.items()))
    lines.append(f"  attribution source: {sources}")

    lines.append("")
    lines.append(f"OPEN POSITIONS ({len(report.positions)})")
    for view in report.positions:
        mark = f"{view.current_price}c" if view.current_price is not None else "NO MARK"
        unreal = f"{view.unrealised:+.2f}" if view.unrealised is not None else "n/a"
        lines.append(
            f"  [{view.action.upper():<5}] {view.market_id}"
        )
        lines.append(
            f"          {view.side} @{view.entry_price}c x{view.quantity} "
            f"(${view.cost_basis}) mark {mark} unrealised {unreal} | "
            f"{view.model} ({view.attribution})"
            + (f" p_model={view.p_model:.3f}" if view.p_model is not None else "")
        )
        lines.append(f"          {view.reason}")

    lines.append("")
    lines.append(
        f"PLAN: close {len(report.to_close)}, flag {len(report.to_flag)}, "
        f"hold {len(report.positions) - len(report.to_close) - len(report.to_flag)}"
    )

    if executed is None:
        lines.append("DRY RUN — nothing was changed.")
    else:
        lines.append("EXECUTED:")
        for result in executed:
            if result["status"] == "closed":
                lines.append(
                    f"  closed {result['market_id']} @ {result['exit_price']}c "
                    f"realized ${result['realized_pnl']:+.2f}"
                )
            else:
                lines.append(f"  {result['market_id']}: {result['status']}")
    return "\n".join(lines)
