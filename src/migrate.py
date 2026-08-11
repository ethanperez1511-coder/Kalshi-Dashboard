"""Explicit schema migration.

    python -m src.migrate            # apply pending additive changes
    python -m src.migrate --check    # report only, exit 1 if behind

Nothing else in the codebase writes schema by default. Booting the API or the
pipeline reports a stale schema and refuses rather than migrating underneath
you — a migration is a deliberate act, not a side effect of an import.
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import Settings, require_production_database
from src.database import ensure_schema, get_engine, pending_schema_changes
from src.run_summary import write_summary

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Apply additive schema migrations.")
    parser.add_argument(
        "--check", action="store_true",
        help="report pending changes without applying them; exit 1 if any",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    require_production_database(settings.DATABASE_URL)
    engine = get_engine(settings.DATABASE_URL)

    pending = pending_schema_changes(engine)
    if not pending:
        logger.info("Schema is up to date — nothing to do.")
        return 0

    if args.check:
        logger.warning("Schema is BEHIND by %d change(s):", len(pending))
        for item in pending:
            logger.warning("  - %s", item)
        return 1

    logger.info("Applying %d schema change(s):", len(pending))
    for item in pending:
        logger.info("  - %s", item)
    added = ensure_schema(engine)
    logger.info("Migration complete. Added: %s", ", ".join(added) or "(tables only)")
    write_summary(
        f"Migration: {len(added)} column(s) added",
        "\n".join(added) or "(new tables only)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
