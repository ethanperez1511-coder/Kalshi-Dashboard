"""Apply the retention policy.

    python -m src.maintenance.prune --dry-run
    python -m src.maintenance.prune
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import Settings, require_production_database
from src.database import get_engine, verify_or_migrate
from src.maintenance.tape import (
    compact_tape,
    format_compaction,
    plan_compaction,
)
from src.maintenance.retention import (
    MB,
    STORAGE_BUDGET_BYTES,
    apply_retention,
    database_size_bytes,
    format_plan,
    live_bytes_estimate,
    plan_retention,
    size_status,
    vacuum_pruned_tables,
)
from src.run_summary import write_summary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply the retention policy.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    require_production_database(settings.DATABASE_URL)
    engine = get_engine(settings.DATABASE_URL)
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="retention")

    plan = plan_retention(engine) if args.dry_run else apply_retention(engine)
    text = format_plan(plan, applied=not args.dry_run)
    print(text)

    # Tape compaction rides the same schedule. It nulls delta payloads that are
    # outside the replay window and redundant with their own columns; trade and
    # snapshot payloads are never touched. Counted in the run summary either
    # way, because a maintenance step nobody can see is one nobody can trust.
    try:
        tape_plan = plan_compaction(engine)
        executed = None if args.dry_run else compact_tape(engine, tape_plan)
        tape_text = format_compaction(tape_plan, executed)
        print("\n" + tape_text)
        text += "\n\n" + tape_text
    except Exception:
        logger.warning("Tape compaction failed (non-fatal)", exc_info=True)
        text += "\n\nTAPE COMPACTION FAILED — see logs"

    # Plain VACUUM after both passes, so the space deletes and payload-nulling
    # freed is reused rather than extended. Measured again afterwards: that is
    # the number the budget is judged on.
    if not args.dry_run:
        try:
            vacuumed = vacuum_pruned_tables(engine)
            plan.size_bytes = database_size_bytes(engine)
            plan.live_bytes = live_bytes_estimate(engine)
            if vacuumed:
                text += (
                    f"\n\nVACUUM (ANALYZE): {', '.join(vacuumed)} — now "
                    f"{size_status(plan.size_bytes, plan.live_bytes)}"
                )
        except Exception:
            logger.error("VACUUM failed — freed space will not be reused", exc_info=True)
            text += "\n\nVACUUM FAILED — see logs"

    # Red means LIVE data over our budget after pruning (dead pages are
    # reused, and are reported, not alarmed), or the file near Neon's cap,
    # which counts dead pages too. Either way it goes red with headroom left.
    over = not args.dry_run and (plan.over_budget or plan.cap_alarm)
    write_summary(
        f"Retention: {size_status(plan.size_bytes, plan.live_bytes)}, "
        f"{plan.total_deletions:,} rows removed",
        text, ok=not over,
    )
    if over:
        logger.error(
            "Over the %.0f MB budget or near the cap after pruning and VACUUM: %s",
            STORAGE_BUDGET_BYTES / MB, size_status(plan.size_bytes, plan.live_bytes),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
