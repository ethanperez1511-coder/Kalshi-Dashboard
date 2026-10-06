"""Maintenance entrypoint. Dry-run by default; executes only on an exact token.

    python -m src.maintenance                       # report only
    python -m src.maintenance --db-stats            # read-only DB census
    python -m src.maintenance --purge-markets      # dry run
    python -m src.maintenance --purge-markets --confirm PURGE-ORPHAN-MARKETS
    python -m src.maintenance --confirm CLOSE-LEGACY-POSITIONS
    python -m src.maintenance --retire-sha e807f8dd
    python -m src.maintenance --retire-sha e807f8dd --confirm RETIRE-DEPLOY-SHA
    python -m src.maintenance --vacuum-full                         # dry run
    python -m src.maintenance --vacuum-full --confirm VACUUM-FULL-TAPE
    python -m src.maintenance --shrink-tape                         # measure, dry run
    python -m src.maintenance --shrink-tape --confirm SHRINK-TAPE
    python -m src.maintenance --wipe-void-shadow                    # dry run
    python -m src.maintenance --wipe-void-shadow --confirm WIPE-VOID-SHADOW

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
from src.maintenance.vacuum_full import (
    CONFIRM_TOKEN as VACUUM_TOKEN,
    execute_vacuum,
    format_plan as format_vacuum,
    plan_vacuum,
)
from src.maintenance.shrink_tape import (
    CONFIRM_TOKEN as SHRINK_TOKEN,
    format_report as format_shrink,
    measure as measure_tape,
    shrink as shrink_tape,
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
        "--vacuum-full", action="store_true",
        help=(
            "Rewrite the pruned tables to return freed space to Neon. "
            f"Dry run unless --confirm {VACUUM_TOKEN}"
        ),
    )
    parser.add_argument(
        "--shrink-tape", action="store_true",
        help=(
            "Measure the tape table; with the token, shrink it in place a chunk "
            f"at a time. Dry run unless --confirm {SHRINK_TOKEN}"
        ),
    )
    parser.add_argument(
        "--wipe-void-shadow", action="store_true",
        help="Delete shadow rows written before the 2026-10-06 rebuild (void). "
             "Dry run unless --confirm WIPE-VOID-SHADOW",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=None,
        help="With --shrink-tape: stop after this many chunks (an installment; no reindex).",
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

    if args.vacuum_full:
        return _vacuum_full(engine, args.confirm.strip())

    if args.wipe_void_shadow:
        return _wipe_void_shadow(engine, args.confirm.strip())

    if args.shrink_tape:
        return _shrink_tape(engine, args.confirm.strip(), args.max_chunks)

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


def _vacuum_full(engine, token: str) -> int:
    """Rewrite the pruned tables. Dry run unless the token matches exactly."""
    if token and token != VACUUM_TOKEN:
        # Covers both a typo and another action's token: neither authorises
        # an exclusive-lock rewrite.
        logger.error(
            "Confirmation token did not match. Expected %r, got %r. "
            "Nothing was changed.", VACUUM_TOKEN, token,
        )
        write_summary("VACUUM FULL: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    plan = plan_vacuum(engine)
    executed = execute_vacuum(engine, plan) if token == VACUUM_TOKEN else None
    text = format_vacuum(plan, executed)
    print(text)

    if executed is not None and plan.refused:
        # Executing and skipping a table is a partial result, so the run must
        # not read green.
        write_summary(
            f"VACUUM FULL: refused {', '.join(plan.refused)} — not enough headroom",
            text[:4000], ok=False,
        )
        return 1

    headline = (
        f"VACUUM FULL EXECUTED: {sum(executed.values()) / 1_000_000:.0f} MB reclaimed"
        if executed is not None else
        f"VACUUM FULL DRY RUN: {len(plan.tables)} tables, "
        f"{len(plan.refused)} refused by the space guard"
    )
    write_summary(headline, text[:4000], ok=True)
    return 0


WIPE_SHADOW_TOKEN = "WIPE-VOID-SHADOW"


def _wipe_void_shadow(engine, token: str) -> int:
    """Delete the shadow rows the pre-rebuild code wrote. Ruling 2026-10-06:
    they were judged before their tape could exist, so they are void. They
    are identified by what the old code never stored (a plan), not by a
    date, so a row placed by the new code can never be caught."""
    from sqlalchemy import delete, func, select

    from src.database import get_session
    from src.models.shadow import ShadowMakerOrder

    if token and token != WIPE_SHADOW_TOKEN:
        logger.error("Confirmation token did not match. Expected %r, got %r. "
                     "Nothing was changed.", WIPE_SHADOW_TOKEN, token)
        write_summary("Wipe void shadow: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    void = ShadowMakerOrder.planned_steps.is_(None)
    with get_session(engine) as session:
        count = session.execute(select(func.count()).select_from(ShadowMakerOrder).where(void)).scalar()
        kept = session.execute(select(func.count()).select_from(ShadowMakerOrder).where(~void)).scalar()
        if token == WIPE_SHADOW_TOKEN:
            session.execute(delete(ShadowMakerOrder).where(void))
            session.commit()
    text = (
        f"WIPE VOID SHADOW {'EXECUTED' if token else 'DRY RUN'}: "
        f"{'deleted' if token else 'would delete'} {count} pre-rebuild rows; "
        f"{kept} rebuilt rows untouched"
    )
    print(text)
    write_summary(text, ok=True)
    return 0


def _shrink_tape(engine, token: str, max_chunks=None) -> int:
    """Measure the tape; shrink it in place only on the exact token."""
    if token and token != SHRINK_TOKEN:
        logger.error(
            "Confirmation token did not match. Expected %r, got %r. "
            "Nothing was changed.", SHRINK_TOKEN, token,
        )
        write_summary("Shrink tape: BAD CONFIRM TOKEN — nothing changed", ok=False)
        return 2

    m = measure_tape(engine)
    result = (
        shrink_tape(engine, m, max_chunks=max_chunks)
        if (token == SHRINK_TOKEN and m.supported) else None
    )
    text = format_shrink(m, result)
    print(text)

    if result is None:
        return publish_report(
            f"Shrink tape DRY RUN: tape {m.tape_file / 1e6:.0f} MB file, "
            f"~{m.live_estimate / 1e6:.0f} MB live",
            text[:4000], substantive=(not m.supported) or m.rows > 0,
        )
    ok = result.refused is None
    write_summary(
        f"Shrink tape {'EXECUTED' if ok else 'REFUSED'}: heap "
        f"{result.heap_before / 1e6:.0f} -> {result.heap_after / 1e6:.0f} MB",
        text[:4000], ok=ok,
    )
    return 0 if ok else 1


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
