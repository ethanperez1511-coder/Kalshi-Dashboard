"""Maintenance entrypoint. Dry-run by default; executes only on an exact token.

    python -m src.maintenance                       # report only
    python -m src.maintenance --confirm CLOSE-LEGACY-POSITIONS

Dispatched from Actions because Neon is only reachable there. Default is
report-only on purpose: a maintenance job that can close positions by being run
is a trading strategy nobody approved, and "I meant to dry-run it" is not a
recoverable mistake once the positions are gone.
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import Settings
from src.database import get_engine, verify_or_migrate
from src.maintenance.legacy_positions import (
    CONFIRM_TOKEN,
    execute_closures,
    format_report,
    reconcile,
)
from src.run_summary import write_summary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile and unwind legacy positions.")
    parser.add_argument("--confirm", default="", help=f"exact token: {CONFIRM_TOKEN}")
    args = parser.parse_args(argv)

    settings = Settings()
    engine = get_engine(settings.DATABASE_URL)
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="maintenance")

    report = reconcile(engine)
    confirmed = args.confirm.strip() == CONFIRM_TOKEN

    if args.confirm and not confirmed:
        # A near-miss token is a typo on a destructive action. Refuse loudly
        # rather than silently falling back to a dry run the operator will
        # mistake for a completed one.
        logger.error(
            "Confirmation token did not match. Expected %r, got %r. "
            "Nothing was changed.", CONFIRM_TOKEN, args.confirm,
        )
        write_summary("Maintenance: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    executed = execute_closures(engine, report) if confirmed else None
    text = format_report(report, executed)
    print(text)

    headline = (
        f"Maintenance EXECUTED: closed {len(executed or [])}"
        if confirmed else
        f"Maintenance DRY RUN: would close {len(report.to_close)}, "
        f"flag {len(report.to_flag)}"
    )
    write_summary(headline, text[:4000], ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
