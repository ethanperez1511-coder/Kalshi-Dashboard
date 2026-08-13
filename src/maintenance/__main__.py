"""Maintenance entrypoint. Dry-run by default; executes only on an exact token.

    python -m src.maintenance                       # report only
    python -m src.maintenance --confirm CLOSE-LEGACY-POSITIONS
    python -m src.maintenance --retire-sha e807f8dd
    python -m src.maintenance --retire-sha e807f8dd --confirm RETIRE-DEPLOY-SHA

The two actions have separate confirmation tokens on purpose. One token for two
destructive operations means confirming either confirms both.

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
    args = parser.parse_args(argv)

    settings = Settings()
    require_production_database(settings.DATABASE_URL)
    engine = get_engine(settings.DATABASE_URL)
    verify_or_migrate(engine, migrate=settings.MIGRATE_ON_BOOT, context="maintenance")

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


if __name__ == "__main__":
    sys.exit(main())


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
