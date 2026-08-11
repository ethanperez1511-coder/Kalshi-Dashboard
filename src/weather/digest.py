"""What the daily digest needs to say about the weather model.

The model declines to price for several different reasons and they are not
interchangeable. A cell that never cleared the gate needs a refit; a stale cell
needs the refit job looked at; a paused cell means live results are
contradicting the offline evidence. Collapsing those into "no weather trades
today" would hide which one is happening.

Denver carries a watch flag: it cleared with the least room (held-out Brier
skill 0.333 at lead 3 against a 0.05 bar), so it is where live drift should
show up first.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

from sqlalchemy import Engine, func, select

from src.database import get_session
from src.models.trade import Trade
from src.weather.archive import archive_depth
from src.weather.fitting import (
    GUARD_MIN_SETTLED,
    all_fits,
    cell_priceable,
    live_brier,
)
from src.weather.promotion import MIN_PAIRS_PER_CELL
from src.weather.stations import STATIONS

# Cleared the gate with the least margin, so drift here is the early warning.
WATCH_STATIONS = ("KXHIGHDEN",)


def weather_digest(engine: Engine, now: dt.datetime = None) -> Dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    fits = all_fits(engine)

    priceable: List[str] = []
    blocked: Dict[str, str] = {}
    below_floor: List[str] = []

    for fit in sorted(fits, key=lambda f: (f.station, f.lead_days)):
        key = f"{fit.station}/L{fit.lead_days}"
        ok, reason = cell_priceable(fit, now)
        if ok:
            priceable.append(key)
        else:
            blocked[key] = reason
        if fit.n_eval_pairs < MIN_PAIRS_PER_CELL:
            below_floor.append(f"{key} ({fit.n_eval_pairs}/{MIN_PAIRS_PER_CELL})")

    # Paper trades per station, so one station cannot stand in for the model.
    with get_session(engine) as session:
        rows = session.execute(
            select(Trade.market_id, func.count())
            .where(Trade.is_paper.is_(True))
            .group_by(Trade.market_id)
        ).all()
    per_station: Dict[str, int] = {}
    for market_id, count in rows:
        series = (market_id or "").split("-")[0]
        if series in STATIONS:
            per_station[series] = per_station.get(series, 0) + count

    watch = {}
    for series in WATCH_STATIONS:
        value, n = live_brier(engine, series)
        watch[series] = {
            "live_brier": value,
            "settled": n,
            "needed": GUARD_MIN_SETTLED,
        }

    return {
        "cells_total": len(fits),
        "cells_priceable": priceable,
        "cells_blocked": blocked,
        "cells_below_floor": below_floor,
        "paper_trades_per_station": per_station,
        "watch": watch,
        "gridpoint_archive": archive_depth(engine, "gridpoint"),
    }


def format_weather_digest(data: Dict[str, Any]) -> str:
    if not data.get("cells_total"):
        return "🌡 Weather: no fitted cells — model cannot price"

    lines = [
        f"🌡 Weather: {len(data['cells_priceable'])}/{data['cells_total']} cells priceable"
    ]
    if data["cells_below_floor"]:
        lines.append(f"   ⚠️ below floor: {', '.join(data['cells_below_floor'])}")
    if data["cells_blocked"]:
        # Group by reason: one refit job failing looks very different from one
        # cell failing its gate.
        grouped: Dict[str, List[str]] = {}
        for cell, reason in data["cells_blocked"].items():
            head = reason.split(":")[0].split(" — ")[0]
            grouped.setdefault(head, []).append(cell)
        for reason, cells in grouped.items():
            shown = ", ".join(cells[:4]) + ("…" if len(cells) > 4 else "")
            lines.append(f"   ✗ {reason}: {shown}")
    if data["paper_trades_per_station"]:
        breakdown = ", ".join(
            f"{k.replace('KXHIGH', '')} {v}"
            for k, v in sorted(data["paper_trades_per_station"].items())
        )
        lines.append(f"   trades by station: {breakdown}")
    for series, w in data.get("watch", {}).items():
        label = series.replace("KXHIGH", "")
        if w["live_brier"] is None:
            lines.append(f"   👁 {label} watch: {w['settled']}/{w['needed']} settled")
        else:
            lines.append(f"   👁 {label} watch: live Brier {w['live_brier']:.3f}")
    depth = data.get("gridpoint_archive") or {}
    if depth.get("rows"):
        lines.append(f"   gridpoint archive: {depth['rows']} rows (challenger history)")
    return "\n".join(lines)
