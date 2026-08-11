"""What the daily digest needs to say about the weather model.

The model declines to price for several different reasons and they are not
interchangeable. A cell that never cleared the gate needs a refit; a stale cell
needs the refit job looked at; a paused cell means live results are
contradicting the offline evidence. Collapsing those into "no weather trades
today" would hide which one is happening.

The watch flag is RANK-BASED, not name-based: it sits on whichever promoted
cell is currently weakest and moves as ranks change. Pinning it to a station
means watching yesterday's problem — Denver was the weakest cell under the
fixed-window fit (0.333 at lead 3) and is mid-pack under the rolling refit,
while KPHL/L3 is now the floor. A station keeps the flag only while it earns it.

Live trailing Brier ranks ahead of held-out skill wherever it exists, since
prospective evidence beats retrospective. Until a cell has settled trades there
is no live number, so held-out skill stands in and the basis is stated.
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

# Station prefix per MOS station, for mapping a cell back to its series.
_SERIES_BY_STATION = {st.mos_station: series for series, st in STATIONS.items()}


def weakest_promoted_cell(engine: Engine):
    """The promoted cell most likely to fail next, and why it is ranked there.

    Live trailing Brier wins where it exists (higher is worse); otherwise
    held-out Brier skill (lower is worse). Returns None when nothing is
    promoted — there is no weakest cell if there are no cells.
    """
    candidates = [f for f in all_fits(engine) if f.promoted]
    if not candidates:
        return None

    ranked = []
    for fit in candidates:
        series = _SERIES_BY_STATION.get(fit.station, fit.station)
        value, n = live_brier(engine, series)
        if value is not None:
            # Live evidence sorts first and worst-first within itself.
            ranked.append((0, -value, fit, series, "live Brier", value, n))
        else:
            skill = fit.brier_skill if fit.brier_skill is not None else 0.0
            ranked.append((1, skill, fit, series, "held-out skill", skill, n))

    ranked.sort(key=lambda r: (r[0], r[1]))
    _, _, fit, series, basis, value, n = ranked[0]
    return {
        "station": fit.station,
        "series": series,
        "lead_days": fit.lead_days,
        "basis": basis,
        "value": value,
        "settled": n,
    }


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

    watch = weakest_promoted_cell(engine)
    if watch:
        watch["needed"] = GUARD_MIN_SETTLED

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
    watch = data.get("watch")
    if watch:
        label = watch["series"].replace("KXHIGH", "")
        if watch["basis"] == "live Brier":
            lines.append(
                f"   👁 weakest: {label}/L{watch['lead_days']} — live Brier "
                f"{watch['value']:.3f} ({watch['settled']} settled)"
            )
        else:
            lines.append(
                f"   👁 weakest: {label}/L{watch['lead_days']} — held-out skill "
                f"{watch['value']:.3f} ({watch['settled']}/{watch['needed']} settled, "
                f"no live number yet)"
            )
    depth = data.get("gridpoint_archive") or {}
    if depth.get("rows"):
        lines.append(f"   gridpoint archive: {depth['rows']} rows (challenger history)")
    return "\n".join(lines)
