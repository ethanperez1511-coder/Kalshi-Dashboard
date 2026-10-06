"""Return the space retention freed to Neon. Dry run by default, token to execute.

DELETE and plain VACUUM make dead space reusable but never shrink a file, and
Neon counts the files. After the first rolling-window prune removes most of the
tape, the database stays at its old size until the tables are rewritten.
`VACUUM FULL` does that rewrite. It is a one-off, not a schedule: once the
files are compact, daily plain VACUUM keeps them that size by reusing pages.

Two costs, stated rather than hidden:

  LOCK   ACCESS EXCLUSIVE on each table for the duration. A recorder or trade
         cycle that touches that table blocks or fails for one run.
  SPACE  the rewrite builds a new copy before dropping the old one, so it needs
         temporary room up to the table's live size. Run this near the cap and
         the cap refuses the copy. The guard below refuses first, and it
         assumes the worst case: a copy as large as the whole current table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import Engine, text

from src.maintenance.retention import (
    MB,
    NEON_CAP_BYTES,
    PRUNED_TABLES,
    database_size_bytes,
    size_status,
)

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "VACUUM-FULL-TAPE"


@dataclass
class TableStat:
    name: str
    total_bytes: int
    live_rows: int
    dead_rows: int


@dataclass
class VacuumPlan:
    supported: bool
    size_before: int = 0
    tables: List[TableStat] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)

    @property
    def headroom(self) -> int:
        return NEON_CAP_BYTES - self.size_before


def plan_vacuum(engine: Engine) -> VacuumPlan:
    """Sizes and dead-row counts, plus which tables the space guard refuses.
    Reads only."""
    if engine.dialect.name != "postgresql":
        return VacuumPlan(supported=False)

    plan = VacuumPlan(supported=True, size_before=database_size_bytes(engine))
    with engine.connect() as conn:
        for table in PRUNED_TABLES:
            row = conn.execute(text(
                "SELECT pg_total_relation_size(c.oid), "
                "       coalesce(s.n_live_tup, 0), coalesce(s.n_dead_tup, 0) "
                "FROM pg_class c "
                "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
                "WHERE c.relname = :t AND c.relkind = 'r'"
            ), {"t": table}).one_or_none()
            if row is None:
                continue
            plan.tables.append(TableStat(table, int(row[0]), int(row[1]), int(row[2])))

    plan.refused = refused_tables(plan.size_before, plan.tables)
    return plan


def refused_tables(size_before: int, tables: List[TableStat]) -> List[str]:
    """Tables whose worst-case rewrite would not fit under the cap.

    Tables are rewritten in order, and the space a rewrite frees is only
    counted once it has finished. The guard therefore assumes nothing comes
    back early: each table must fit, as a full copy, in the headroom that
    exists now.
    """
    headroom = NEON_CAP_BYTES - size_before
    return [t.name for t in tables if t.total_bytes >= headroom]


def execute_vacuum(engine: Engine, plan: VacuumPlan) -> Dict[str, int]:
    """Rewrite each table the guard allows. Returns bytes reclaimed per table."""
    reclaimed: Dict[str, int] = {}
    for table in plan.tables:
        if table.name in plan.refused:
            continue
        before = database_size_bytes(engine)
        # VACUUM cannot run inside a transaction block.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            logger.info("VACUUM (FULL, ANALYZE) %s — ACCESS EXCLUSIVE lock", table.name)
            conn.execute(text(f"VACUUM (FULL, ANALYZE) {table.name}"))
        reclaimed[table.name] = before - database_size_bytes(engine)
    return reclaimed


def format_plan(plan: VacuumPlan, executed: Optional[Dict[str, int]] = None) -> str:
    if not plan.supported:
        return "VACUUM FULL: not a Postgres database — nothing to do"

    lines = [
        "VACUUM FULL " + ("EXECUTED" if executed is not None else "DRY RUN"),
        "",
        f"  database           : {size_status(plan.size_before)}",
        f"  headroom to cap    : {plan.headroom / MB:.0f} MB",
        "",
        f"  {'table':24s} {'size':>10s} {'live rows':>12s} {'dead rows':>12s}",
    ]
    for t in plan.tables:
        mark = "  REFUSED: worst-case copy exceeds headroom" if t.name in plan.refused else ""
        lines.append(
            f"  {t.name:24s} {t.total_bytes / MB:>8.0f} MB {t.live_rows:>12,} "
            f"{t.dead_rows:>12,}{mark}"
        )
    if executed is not None:
        lines.append("")
        for name, freed in executed.items():
            lines.append(f"  reclaimed {name:24s} {freed / MB:>8.0f} MB")
        lines.append(f"\n  now: {size_status(plan.size_before - sum(executed.values()))}")
    else:
        lines += [
            "",
            f"  To execute, re-run with confirm={CONFIRM_TOKEN}.",
            "  Each table holds an ACCESS EXCLUSIVE lock while it is rewritten,",
            "  so a recorder or trade run that overlaps it blocks or loses one tick.",
        ]
    return "\n".join(lines)
