"""Record every forecast we see, every day, traded on or not.

Two reasons this runs unconditionally:

  MIGRATION   MOS reaches us through a third-party archive with no SLA. The
              only way off that dependency is to have our own history, and a
              day not recorded is gone — unlike a fit, it cannot be
              reconstructed later.

  CHALLENGER  NWS gridpoint is a registered challenger to MOS, but it publishes
              no archive at all, so its history exists only if we build it.
              Until then there is nothing to fit it on and no way to run it
              against the incumbent's bar.

Failures here are logged and swallowed. Archiving is bookkeeping: it must never
be able to stop a trading cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, List, Optional

import httpx
from sqlalchemy import Engine

from src.database import get_session
from src.models.weather import ForecastArchive
from src.weather import mos
from src.weather.stations import STATIONS, Station

logger = logging.getLogger(__name__)

# NWS requires a User-Agent and answers 403 Access Denied without one — not a
# 400, so a missing header looks like a permissions problem rather than a
# malformed request.
_NWS_HEADERS = {"User-Agent": "kalshi-trading-bot (research; ethanperez1511@gmail.com)"}
_NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"


def _store(engine: Engine, rows: Iterable[dict]) -> int:
    written = 0
    with get_session(engine) as session:
        for row in rows:
            existing = (
                session.query(ForecastArchive)
                .filter_by(
                    station=row["station"], target_date=row["target_date"],
                    lead_days=row["lead_days"], source=row["source"],
                )
                .first()
            )
            if existing is not None:
                continue  # first observation of a forecast is the honest one
            session.add(ForecastArchive(**row))
            written += 1
        session.commit()
    return written


# MEX publishes twice daily. Archiving only 12Z writes nothing for the hours
# before it posts — caught by smoke test, where a run late in the UTC morning
# recorded 56 gridpoint rows and zero MOS. Both runs are recorded, tagged by
# run hour, so fitting can still select a single cadence while the archive
# keeps everything that was actually available.
MOS_RUN_HOURS = (12, 0)


def archive_mos(engine: Engine, station: Station, http=httpx, now=None) -> int:
    """Record today's MOS runs for one station, at every lead they publish."""
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    written = 0
    for hour in MOS_RUN_HOURS:
        runtime = dt.datetime(today.year, today.month, today.day, hour,
                              tzinfo=dt.timezone.utc)
        try:
            forecasts = mos.fetch_run(station.mos_station, runtime, http=http)
        except mos.MosUnavailable as exc:
            logger.debug(
                "Archive: no %02dZ MOS for %s today (%s)",
                hour, station.mos_station, exc,
            )
            continue
        written += _store(engine, [
            {
                "station": station.mos_station, "target_date": f.target_date,
                "lead_days": f.lead_days, "source": f"mos_{hour:02d}z",
                "max_temp_f": f.max_temp_f, "runtime": f.runtime,
            }
            for f in forecasts
        ])
    if not written:
        logger.info("Archive: no MOS runs available for %s today", station.mos_station)
    return written


def fetch_gridpoint_daily_max(
    station: Station, http=httpx, timeout: float = 30.0,
) -> List[dict]:
    """NWS gridpoint daily maxima. Deterministic — no quantiles are published.

    Two hops: /points resolves a lat/lon to a grid, then the grid serves the
    forecast. Any failure returns [] — this is archival, not pricing.
    """
    try:
        point = http.get(
            _NWS_POINTS.format(lat=station.latitude, lon=station.longitude),
            headers=_NWS_HEADERS, timeout=timeout,
        )
        if point.status_code != 200:
            logger.info("Archive: NWS points %s -> %s", station.mos_station, point.status_code)
            return []
        grid_url = point.json()["properties"]["forecastGridData"]

        grid = http.get(grid_url, headers=_NWS_HEADERS, timeout=timeout)
        if grid.status_code != 200:
            logger.info("Archive: NWS grid %s -> %s", station.mos_station, grid.status_code)
            return []
        values = grid.json()["properties"]["maxTemperature"]["values"]
    except Exception:
        logger.info("Archive: NWS gridpoint failed for %s", station.mos_station, exc_info=True)
        return []

    out: List[dict] = []
    for entry in values:
        try:
            stamp = str(entry["validTime"]).split("/")[0]
            target = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
            celsius = float(entry["value"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"target_date": target, "max_temp_f": celsius * 9.0 / 5.0 + 32.0})
    return out


def archive_gridpoint(engine: Engine, station: Station, http=httpx, now=None) -> int:
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    rows = []
    for entry in fetch_gridpoint_daily_max(station, http=http):
        lead = (entry["target_date"] - today).days
        if lead < 0 or lead > mos.MAX_LEAD_DAYS:
            continue
        rows.append({
            "station": station.mos_station, "target_date": entry["target_date"],
            "lead_days": lead, "source": "gridpoint",
            "max_temp_f": entry["max_temp_f"], "runtime": None,
        })
    return _store(engine, rows)


def run_daily_archive(engine: Engine, http=httpx, now=None) -> dict:
    """Archive both predictors for every station. Never raises."""
    counts = {"mos": 0, "gridpoint": 0, "errors": 0}
    for station in STATIONS.values():
        for source, fn in (("mos", archive_mos), ("gridpoint", archive_gridpoint)):
            try:
                counts[source] += fn(engine, station, http=http, now=now)
            except Exception:
                counts["errors"] += 1
                logger.warning(
                    "Archive: %s failed for %s", source, station.mos_station,
                    exc_info=True,
                )
    logger.info(
        "Forecast archive: %d MOS, %d gridpoint rows written (%d errors)",
        counts["mos"], counts["gridpoint"], counts["errors"],
    )
    return counts


def archive_depth(engine: Engine, source: str = "gridpoint") -> dict:
    """How close a challenger is to having enough history to be fitted."""
    from sqlalchemy import func, select

    with get_session(engine) as session:
        row = session.execute(
            select(
                func.count(ForecastArchive.id),
                func.min(ForecastArchive.target_date),
                func.max(ForecastArchive.target_date),
            ).where(ForecastArchive.source == source)
        ).first()
    return {"rows": row[0] or 0, "first": row[1], "last": row[2]}
