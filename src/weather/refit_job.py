"""Refit every weather cell and re-run the unchanged promotion gate.

    python -m src.weather.refit_job                # refit all cells
    python -m src.weather.refit_job --days 240     # deeper backfill
    python -m src.weather.refit_job --dry-run      # report, write nothing

Run on a schedule, not on the trading path. The validated design refits on a
trailing 90-day window before pricing, and `cell_priceable` refuses any fit
older than its cadence — so a cell whose refit stops running goes unpriceable
rather than quietly pricing off parameters nobody measured.

Nothing here can loosen anything: it fits, scores on held-out pairs, and calls
the same `evaluate_promotion` the offline harness used, with the same bars.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import logging
import sys
from typing import Dict, List, Tuple

import httpx

from src.config import Settings
from src.database import get_engine, verify_or_migrate
from src.run_summary import write_summary
from src.weather.calibration import Pair
from src.weather.fitting import REFIT_WINDOW_DAYS, refit_cell
from src.weather.mos import MosUnavailable, fetch_run
from src.weather.promotion import MIN_PAIRS_PER_CELL
from src.weather.stations import STATIONS, Station
from src.weather.truth import TruthUnavailable, fetch_daily_max

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

LEADS = (1, 2, 3)
# Enough that the trailing 90-day window is full AND there are held-out pairs
# beyond it to score on. Anything less and every cell fails the sample floor.
DEFAULT_BACKFILL_DAYS = 240
RUN_HOUR = 12
MAX_WORKERS = 10


def _fetch_forecasts(
    station: Station, start: dt.date, end: dt.date, http,
) -> Dict[Tuple[int, dt.date], float]:
    """MOS daily maxima for one station, keyed by (lead, target date)."""
    run_dates: List[dt.date] = []
    day = start - dt.timedelta(days=max(LEADS) + 1)
    while day <= end:
        run_dates.append(day)
        day += dt.timedelta(days=1)

    out: Dict[Tuple[int, dt.date], float] = {}

    def one(run_date: dt.date):
        runtime = dt.datetime(
            run_date.year, run_date.month, run_date.day, RUN_HOUR,
            tzinfo=dt.timezone.utc,
        )
        try:
            return fetch_run(station.mos_station, runtime, http=http)
        except MosUnavailable:
            return []          # a missing run is a gap, not a failure

    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for batch in pool.map(one, run_dates):
            for forecast in batch:
                if forecast.lead_days in LEADS:
                    out[(forecast.lead_days, forecast.target_date)] = forecast.max_temp_f
    return out


def refit_all(engine, days: int = DEFAULT_BACKFILL_DAYS, dry_run: bool = False) -> dict:
    today = dt.date.today()
    end = today - dt.timedelta(days=2)      # NCEI publishes with ~2 days latency
    start = end - dt.timedelta(days=days)
    # Fit history first, hold out the tail. Same discipline as the harness.
    split = end - dt.timedelta(days=days // 3)

    http = httpx.Client(
        headers={"User-Agent": "kalshi-trading-bot (research)"}, timeout=45.0,
    )
    summary = {"promoted": [], "rejected": [], "skipped": []}

    try:
        for series, station in STATIONS.items():
            try:
                truth = fetch_daily_max(station.ghcn_id, start, end, http=http)
            except TruthUnavailable as exc:
                logger.warning("%s: no truth series (%s) — skipped", series, exc)
                summary["skipped"].append(f"{series}: truth unavailable")
                continue

            forecasts = _fetch_forecasts(station, start, end, http)
            if not forecasts:
                logger.warning("%s: no MOS history — skipped", series)
                summary["skipped"].append(f"{series}: no MOS history")
                continue

            for lead in LEADS:
                pairs = [
                    Pair(station.mos_station, lead, target, value, truth[target])
                    for (lead_days, target), value in forecasts.items()
                    if lead_days == lead and target in truth
                ]
                label = f"{station.mos_station}/L{lead}"
                if len(pairs) < MIN_PAIRS_PER_CELL:
                    logger.info(
                        "%s: %d pairs < floor %d — left unpriceable",
                        label, len(pairs), MIN_PAIRS_PER_CELL,
                    )
                    summary["skipped"].append(f"{label}: {len(pairs)} pairs")
                    continue

                if dry_run:
                    summary["skipped"].append(f"{label}: dry-run ({len(pairs)} pairs)")
                    continue

                stored = refit_cell(engine, pairs, station.mos_station, lead, split)
                if stored is None:
                    summary["skipped"].append(f"{label}: refit produced nothing")
                elif stored.promoted:
                    summary["promoted"].append(
                        f"{label} BSS={stored.brier_skill:.3f} "
                        f"slope={stored.reliability_slope:.3f} n={stored.n_eval_pairs}"
                    )
                else:
                    summary["rejected"].append(f"{label}: {stored.rejection_reasons}")
    finally:
        http.close()

    logger.info(
        "Refit complete: %d promoted, %d rejected, %d skipped",
        len(summary["promoted"]), len(summary["rejected"]), len(summary["skipped"]),
    )
    for line in summary["promoted"]:
        logger.info("  PROMOTED %s", line)
    for line in summary["rejected"]:
        logger.info("  rejected %s", line)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refit weather cells.")
    parser.add_argument("--days", type=int, default=DEFAULT_BACKFILL_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.days < REFIT_WINDOW_DAYS + MIN_PAIRS_PER_CELL:
        logger.warning(
            "--days %d is too short to fill a %d-day trailing window and still "
            "hold out pairs; every cell will fail the sample floor",
            args.days, REFIT_WINDOW_DAYS,
        )

    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    # Same discipline as the pipeline: report a stale schema and refuse rather
    # than failing later with a raw driver error that says nothing about the fix.
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="the weather refit job")
    summary = refit_all(engine, days=args.days, dry_run=args.dry_run)
    ok = bool(summary["promoted"] or args.dry_run)
    write_summary(
        f"Weather refit: {len(summary['promoted'])} promoted, "
        f"{len(summary['rejected'])} rejected, {len(summary['skipped'])} skipped",
        "\n".join(summary["promoted"][:25]) or "no cells promoted",
        ok=ok,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
