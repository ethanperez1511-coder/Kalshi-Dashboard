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
from src.maintenance.retention import (
    EMERGENCY_FRACTION,
    apply_retention,
    format_plan,
    plan_retention,
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

    # Still over the emergency line after pruning means ingest is outrunning
    # retention, which retention cannot fix by trying harder.
    over = plan.fraction >= EMERGENCY_FRACTION and not args.dry_run
    write_summary(
        f"Retention: {plan.size_bytes/1e6:.0f} MB ({plan.fraction:.0%} of tier), "
        f"{plan.total_deletions:,} rows removed",
        text, ok=not over,
    )
    if over:
        logger.error(
            "Still above %.0f%% after pruning — the write rate, not retention, "
            "is the problem", EMERGENCY_FRACTION * 100,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
