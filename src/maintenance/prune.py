"""Apply the retention policy.

    python -m src.maintenance.prune --dry-run
    python -m src.maintenance.prune
"""
from __future__ import annotations

import argparse
import datetime as dt
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
    apply_retention,
    database_size_bytes,
    defend_cap,
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

    failed_steps = []
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
        # Not "non-fatal" any more: the 2-day payload rule is how the tape
        # stays inside the cap, and this failed silently from August to
        # 2026-10-12 (a NOT NULL left on the column). A failed step is red.
        logger.error("Tape compaction FAILED", exc_info=True)
        text += "\n\nTAPE COMPACTION FAILED — see logs"
        failed_steps.append("tape compaction")

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
            failed_steps.append("VACUUM")

    # Cap defense (ruling C). Alerts go to Telegram on every step.
    if not args.dry_run:
        try:
            from src.alerts import Alerter

            alerter = Alerter()
            steps = defend_cap(engine, dt.datetime.now(dt.timezone.utc), alerter.send)
            if steps:
                plan.size_bytes = database_size_bytes(engine)
                plan.live_bytes = live_bytes_estimate(engine)
                text += "\n\nCAP DEFENSE:\n  " + "\n  ".join(steps)
        except Exception:
            logger.error("Cap defense failed", exc_info=True)
            text += "\n\nCAP DEFENSE FAILED — see logs"
            failed_steps.append("cap defense")

    # Per-table sizes on every run, so a jump can be attributed from the run
    # summary instead of guessed (2026-10-13: +89 MB in six hours, unattributed).
    if engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text as _sql

            with engine.connect() as conn:
                rows = conn.execute(_sql(
                    "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'r' AND n.nspname = 'public' "
                    "ORDER BY 2 DESC LIMIT 8"
                )).all()
            text += "\n\nTABLES (total incl. indexes):\n" + "\n".join(
                f"  {name:28s} {size / MB:>8.1f} MB" for name, size in rows
            )
        except Exception:
            logger.warning("Per-table sizes unavailable", exc_info=True)

    # Red means one of two things: the file is at the fail line of Neon's
    # cap, or a retention step failed. Never "over a budget" while healthy.
    over = not args.dry_run and plan.cap_fail
    red = over or bool(failed_steps)
    headline = f"Retention: {size_status(plan.size_bytes, plan.live_bytes)}, {plan.total_deletions:,} rows removed"
    if failed_steps:
        headline += f" — FAILED: {', '.join(failed_steps)}"
    write_summary(headline, text, ok=not red)
    if over:
        logger.error("File at the %.0f%% fail line of the Neon cap: %s",
                     100 * plan.cap_fraction, size_status(plan.size_bytes, plan.live_bytes))
    if failed_steps:
        logger.error("Retention steps failed: %s", ", ".join(failed_steps))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
