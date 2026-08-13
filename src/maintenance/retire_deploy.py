"""Retire the trades a named deploy produced, and unwind what it left open.

The 50-trade gate measures the system that would go live. A deploy whose
NO-side expected value was computed with the win and loss amounts swapped —
reporting +0.85 on a bet worth +0.03, and choosing the side on that basis — did
not produce evidence about that system. Deploy e807f8dd is the first such case.

Distinct from `mark_legacy_trades`, which retires rows that predate deploy
tracking. This retires rows whose deploy tracked itself correctly and was still
wrong, and it is necessarily a named, human-dispatched decision rather than a
rule: no invariant in the code can know that a formula was wrong.

History is kept. Rows stay, positions get a real close at the current mark
through the ordinary settlement path, and only their standing changes.

Dry run by default, and the confirmation token is its own — deliberately not
the one that unwinds legacy positions, because two destructive actions sharing
a token means confirming one confirms the other.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import Engine, select

from src.database import get_session
from src.models.position import Position
from src.models.trade import Trade

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "RETIRE-DEPLOY-SHA"


@dataclass
class RetirementPlan:
    shas: List[str] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    open_positions: List[dict] = field(default_factory=list)
    gate_before: int = 0
    gate_after: int = 0
    unmarked: List[str] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        return len(self.trades)


def _matches(deploy_sha: Optional[str], prefixes: Sequence[str]) -> bool:
    return bool(deploy_sha) and deploy_sha.startswith(tuple(prefixes))


def plan_retirement(engine: Engine, shas: Sequence[str]) -> RetirementPlan:
    """What retiring these deploys would do. Writes nothing."""
    from src.legacy_cutoff import gate_count

    prefixes = [s.strip() for s in shas if s and s.strip()]
    plan = RetirementPlan(shas=prefixes)
    plan.gate_before = gate_count(engine)
    plan.gate_after = plan.gate_before
    if not prefixes:
        # An empty selector must be a no-op, not a wildcard. Getting this
        # backwards would void the entire gate on a blank input.
        return plan

    with get_session(engine) as session:
        rows = session.execute(
            select(Trade.market_id, Trade.side, Trade.price, Trade.quantity,
                   Trade.deploy_sha, Trade.is_legacy, Trade.is_paper,
                   Trade.model_name, Trade.traded_edge, Trade.net_ev)
        ).all()
        open_rows = session.execute(
            select(Position.market_id, Position.side, Position.entry_price,
                   Position.quantity, Position.current_price)
            .where(Position.status == "open")
        ).all()

    matched_markets = set()
    for (market_id, side, price, qty, sha, is_legacy, is_paper,
         model, traded_edge, net_ev) in rows:
        if not _matches(sha, prefixes):
            continue
        matched_markets.add(market_id)
        plan.trades.append({
            "market_id": market_id, "side": side, "price": price,
            "quantity": qty, "deploy_sha": (sha or "")[:8],
            "already_legacy": bool(is_legacy), "is_paper": bool(is_paper),
            "model": model, "traded_edge": traded_edge, "net_ev": net_ev,
        })

    plan.gate_after = plan.gate_before - sum(
        1 for t in plan.trades if t["is_paper"] and not t["already_legacy"]
    )

    for market_id, side, entry, qty, current in open_rows:
        if market_id not in matched_markets:
            continue
        mark = _side_mark(engine, market_id, side)
        plan.open_positions.append({
            "market_id": market_id, "side": side, "entry_price": entry,
            "quantity": qty, "mark": mark,
            "cost_basis": round(entry * qty / 100.0, 2),
            "unrealised": (
                round((mark - entry) * qty / 100.0, 2) if mark is not None else None
            ),
        })
        if mark is None:
            # No mark, no honest close. Naming it beats closing at a number we
            # made up, and beats silently leaving it open.
            plan.unmarked.append(market_id)

    return plan


def _side_mark(engine: Engine, market_id: str, side: str) -> Optional[int]:
    from src.maintenance.legacy_positions import _current_yes_price

    mark_yes = _current_yes_price(engine, market_id)
    if mark_yes is None:
        return None
    return mark_yes if side == "yes" else 100 - mark_yes


def execute_retirement(engine: Engine, plan: RetirementPlan) -> Dict[str, object]:
    """Apply the plan: close positions at their mark, then retire the rows.

    Order matters. Closing first means the close writes through the ordinary
    settlement path — same trade rows, same bankroll movement, same fee
    handling as any other close — and the retirement marking that follows
    sweeps up whatever the close created. Retiring first would leave the
    closing trade counting toward the gate.
    """
    from src.legacy_cutoff import mark_legacy_by_sha, resync_gate_counter
    from src.portfolio.tracker import PortfolioTracker

    closed: List[dict] = []
    if plan.shas:
        tracker = PortfolioTracker(engine)
        for position in plan.open_positions:
            if position["mark"] is None:
                closed.append({"market_id": position["market_id"],
                               "status": "no_mark_left_open"})
                continue
            # close_position takes a YES-scale exit and converts to side terms.
            exit_yes = (
                position["mark"] if position["side"] == "yes"
                else 100 - position["mark"]
            )
            result = tracker.close_position(
                position["market_id"], exit_yes, finalize_market=False,
            )
            if result is None:
                closed.append({"market_id": position["market_id"],
                               "status": "not_found"})
                continue
            closed.append({
                "market_id": position["market_id"], "status": "closed",
                "exit_price": result["exit_price"],
                "realized_pnl": result["realized_pnl"],
            })
            logger.info(
                "Retired position %s closed at %dc — realized $%.2f",
                position["market_id"], result["exit_price"], result["realized_pnl"],
            )

    retired = mark_legacy_by_sha(engine, plan.shas)
    # And again for anything the closes just wrote against those markets.
    retired += _retire_trades_for_markets(
        engine, [t["market_id"] for t in plan.trades],
    )
    gate = resync_gate_counter(engine)
    return {"closed": closed, "retired": retired, "gate_count": gate}


def _retire_trades_for_markets(engine: Engine, market_ids: Sequence[str]) -> int:
    """Retire any remaining non-legacy trade on a retired market.

    The close writes a trade row of its own, and that row has the CURRENT
    deploy SHA rather than the retired one. Left alone it would count toward
    the gate — an unwind of discredited evidence becoming evidence itself.
    """
    if not market_ids:
        return 0
    targets = set(market_ids)
    with get_session(engine) as session:
        rows = session.query(Trade).filter(Trade.is_legacy.is_(False)).all()
        marked = [r for r in rows if r.market_id in targets]
        for row in marked:
            row.is_legacy = True
        session.commit()
        return len(marked)


def format_plan(plan: RetirementPlan, executed: Optional[Dict] = None) -> str:
    lines = [
        "RETIRE DEPLOY",
        "=" * 60,
        f"deploy SHAs      : {', '.join(plan.shas) or '(none given — no-op)'}",
        f"trades matched   : {plan.trade_count}",
        f"gate count       : {plan.gate_before} -> {plan.gate_after}",
        "",
    ]
    for t in plan.trades:
        edge = "n/a" if t["traded_edge"] is None else f"{t['traded_edge']:+.4f}"
        flag = "  [already legacy]" if t["already_legacy"] else ""
        lines.append(
            f"  {t['market_id']}  {t['side'].upper()} x{t['quantity']} @ "
            f"{t['price']}c  {t['model'] or '?'}  traded_edge={edge}{flag}"
        )
    if plan.open_positions:
        lines += ["", "OPEN POSITIONS TO CLOSE AT MARK:"]
        for p in plan.open_positions:
            mark = "NO MARK" if p["mark"] is None else f"{p['mark']}c"
            unreal = "n/a" if p["unrealised"] is None else f"${p['unrealised']:+.2f}"
            lines.append(
                f"  {p['market_id']}  {p['side'].upper()} x{p['quantity']} "
                f"entry {p['entry_price']}c  mark {mark}  unrealised {unreal}"
            )
    if plan.unmarked:
        lines += ["", "!! NO MARK AVAILABLE — left open rather than closed at a made-up price:"]
        lines += [f"  {m}" for m in plan.unmarked]

    if executed is None:
        lines += ["", "DRY RUN — nothing was changed.",
                  f"Re-run with --confirm {CONFIRM_TOKEN} to apply."]
    else:
        lines += ["", "EXECUTED", "-" * 60,
                  f"trades retired   : {executed['retired']}",
                  f"gate count now   : {executed['gate_count']}"]
        for c in executed["closed"]:
            if c["status"] == "closed":
                lines.append(
                    f"  closed {c['market_id']} at {c['exit_price']}c — "
                    f"realized ${c['realized_pnl']:+.2f}"
                )
            else:
                lines.append(f"  {c['market_id']}: {c['status']}")
    return "\n".join(lines)
