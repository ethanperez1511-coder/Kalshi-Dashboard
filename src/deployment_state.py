"""The ground-truth snapshot: what is deployed, migrated, live and counting.

Every number here answers a question that was previously answered by assumption
and turned out to be wrong. The deploy SHA because thirteen commits sat unpushed
while reports said shipped. Migration status because a stale schema fails on the
first query touching a missing column. Weather cells and recorder hours because
"the workflow exists" and "the workflow has run" are different facts. The gate
counter because it now counts a different population than it used to.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from sqlalchemy import Engine

from src.database import pending_schema_changes
from src.legacy_cutoff import gate_count, legacy_count


def deployment_state(engine: Engine) -> Dict[str, Any]:
    sha = os.environ.get("GITHUB_SHA", "")

    try:
        pending = pending_schema_changes(engine)
    except Exception as exc:
        pending = [f"unreadable: {exc}"]

    try:
        from src.weather.fitting import all_fits, cell_priceable

        fits = all_fits(engine)
        priceable = sum(1 for f in fits if cell_priceable(f)[0])
    except Exception:
        fits, priceable = [], 0

    try:
        from src.recorder.health import recorder_health

        health = recorder_health(engine)
    except Exception:
        health = {"messages": 0, "per_category": {}}

    from src.models.settings import TradingSettings
    from src.database import get_session

    with get_session(engine) as session:
        settings = session.query(TradingSettings).first()
        target = settings.paper_trades_before_live if settings else 50

    return {
        "deploy_sha": sha[:8] if sha else None,
        "deployed": bool(sha),
        "pending_migrations": pending,
        "weather_cells_priceable": priceable,
        "weather_cells_total": len(fits),
        "recorder_messages": health.get("messages", 0),
        "recorder_hours_by_category": {
            category: stats.get("hours", 0)
            for category, stats in health.get("per_category", {}).items()
        },
        "gate_count": gate_count(engine),
        "gate_target": target,
        "legacy_excluded": legacy_count(engine),
    }


def format_deployment_state(state: Dict[str, Any]) -> str:
    lines = ["📦 <b>Deployment state</b>"]

    if state["deployed"]:
        lines.append(f"   commit {state['deploy_sha']}")
    else:
        lines.append("   ⚠️ no GITHUB_SHA — not a deployed run")

    pending = state["pending_migrations"]
    lines.append(
        "   migrations: up to date" if not pending
        else f"   ⚠️ migrations PENDING: {', '.join(pending[:4])}"
    )

    total = state["weather_cells_total"]
    lines.append(
        f"   weather: {state['weather_cells_priceable']}/{total} cells priceable"
        if total else "   weather: no fits — refit has not run"
    )

    hours = state["recorder_hours_by_category"]
    if state["recorder_messages"]:
        breakdown = ", ".join(f"{c} {h}h" for c, h in sorted(hours.items()))
        lines.append(f"   recorder: {state['recorder_messages']} msgs — {breakdown}")
    else:
        lines.append("   ⚠️ recorder: no data — N clock has not started")

    lines.append(
        f"   gate: {state['gate_count']}/{state['gate_target']} "
        f"({state['legacy_excluded']} legacy excluded)"
    )
    return "\n".join(lines)
