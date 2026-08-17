"""Maintenance entrypoint. Dry-run by default; executes only on an exact token.

    python -m src.maintenance                       # report only
    python -m src.maintenance --db-stats            # read-only DB census
    python -m src.maintenance --purge-markets      # dry run
    python -m src.maintenance --purge-markets --confirm PURGE-ORPHAN-MARKETS
    python -m src.maintenance --confirm CLOSE-LEGACY-POSITIONS
    python -m src.maintenance --retire-sha e807f8dd
    python -m src.maintenance --retire-sha e807f8dd --confirm RETIRE-DEPLOY-SHA

The two destructive actions have separate confirmation tokens on purpose. One
token for two destructive operations means confirming either confirms both.

`--db-stats` takes no token because it cannot change anything. Requiring one to
read the size of a database that is filling up would only guarantee nobody runs
it while it matters.

Dispatched from Actions because Neon is only reachable there. Default is
report-only on purpose: a maintenance job that can close positions by being run
is a trading strategy nobody approved, and "I meant to dry-run it" is not a
recoverable mistake once the positions are gone.
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import Settings, require_production_database
from src.database import get_engine, verify_or_migrate
from src.maintenance.db_stats import collect as collect_db_stats, format_report as format_db_stats
from src.maintenance.purge_markets import (
    CONFIRM_TOKEN as PURGE_TOKEN,
    execute_purge,
    format_plan as format_purge,
    plan_purge,
)
from src.maintenance.legacy_positions import (
    CONFIRM_TOKEN,
    execute_closures,
    format_report,
    reconcile,
)
from src.maintenance.retire_deploy import (
    CONFIRM_TOKEN as RETIRE_TOKEN,
    execute_retirement,
    format_plan as format_retirement,
    plan_retirement,
)
from src.report_guard import publish_report
from src.run_summary import write_summary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile and unwind legacy positions.")
    parser.add_argument("--confirm", default="", help=f"exact token: {CONFIRM_TOKEN}")
    parser.add_argument(
        "--retire-sha", default=None, action="append", dest="retire_shas",
        help=(
            "Retire every trade produced by this deploy SHA (prefix match) from "
            f"the gate and from calibration. Confirm token: {RETIRE_TOKEN}"
        ),
    )
    parser.add_argument(
        "--purge-markets", action="store_true",
        help=(
            "Delete market rows with zero dependent data and archive the rest. "
            f"Dry run unless --confirm {PURGE_TOKEN}"
        ),
    )
    parser.add_argument(
        "--db-stats", action="store_true",
        help="Read-only census of table sizes, market statuses and growth rates.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    require_production_database(settings.DATABASE_URL)
    engine = get_engine(settings.DATABASE_URL)
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="maintenance")

    if args.db_stats:
        return _db_stats(engine)

    if args.purge_markets:
        return _purge(engine, args.confirm.strip())

    shas = [s for s in (args.retire_shas or []) if s and s.strip()]
    if shas:
        return _retire(engine, shas, args.confirm.strip())

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

def _db_stats(engine) -> int:
    """Print the census and put it on the Actions summary. Changes nothing.

    A census of a database with no rows in it is not a census — it is a
    connection to the wrong place, or a schema that never migrated. Either way
    it must not read as a healthy green check.
    """
    stats = collect_db_stats(engine)
    return publish_report(
        f"DB census: {stats.open_markets:,} open markets, "
        f"{stats.markets_with_fresh_snapshot:,} reachable by the scorer",
        format_db_stats(stats),
        substantive=any(stats.row_counts.values()),
    )


def _purge(engine, token: str) -> int:
    """Reclaim the parlay graveyard. Dry run unless the token matches exactly."""
    plan = plan_purge(engine)

    if token and token != PURGE_TOKEN:
        # A near-miss token on a destructive action is a typo, not consent.
        # Falling back to a dry run would hand back a report the operator reads
        # as a completed purge.
        logger.error(
            "Confirmation token did not match. Expected %r, got %r. "
            "Nothing was changed.", PURGE_TOKEN, token,
        )
        write_summary("Purge markets: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    if token in (CONFIRM_TOKEN, RETIRE_TOKEN):
        # Neither of the other two tokens authorises deleting rows.
        logger.error(
            "Refusing: %r does not confirm a market purge. Use %r.",
            token, PURGE_TOKEN,
        )
        write_summary("Purge markets: WRONG TOKEN for this action", ok=False)
        return 2

    executed = execute_purge(engine, plan) if token == PURGE_TOKEN else None
    text = format_purge(plan, executed)
    print(text)

    headline = (
        f"Purge EXECUTED: {executed['deleted']:,} deleted, "
        f"{executed['archived']:,} archived"
        if executed else
        f"Purge DRY RUN: would delete {plan.deletable:,}, "
        f"archive {plan.archivable:,}, exempt {plan.exempt:,}"
    )
    write_summary(headline, text[:4000], ok=True)
    return 0


def _retire(engine, shas, token: str) -> int:
    """Retire named deploys. Dry run unless the token matches exactly."""
    plan = plan_retirement(engine, shas)

    if token and token != RETIRE_TOKEN:
        # A near-miss token on a destructive action is a typo, not consent.
        # Falling back to a dry run here would produce a report the operator
        # reads as a completed retirement.
        logger.error(
            "Confirmation token did not match. Expected %r, got %r. "
            "Nothing was changed.", RETIRE_TOKEN, token,
        )
        write_summary("Retire deploy: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    if token == CONFIRM_TOKEN:
        # The position-unwind token must not authorise a gate reset.
        logger.error(
            "Refusing: %r confirms the legacy-position unwind, not a deploy "
            "retirement. Use %r.", CONFIRM_TOKEN, RETIRE_TOKEN,
        )
        write_summary("Retire deploy: WRONG TOKEN for this action", ok=False)
        return 2

    executed = execute_retirement(engine, plan) if token == RETIRE_TOKEN else None
    text = format_retirement(plan, executed)
    print(text)

    headline = (
        f"Retired deploy {','.join(plan.shas)}: {plan.trade_count} trades, "
        f"gate now {executed['gate_count']}"
        if executed else
        f"Retire deploy DRY RUN {','.join(plan.shas)}: would retire "
        f"{plan.trade_count} trades, gate {plan.gate_before} -> {plan.gate_after}"
    )
    write_summary(headline, text[:4000], ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
