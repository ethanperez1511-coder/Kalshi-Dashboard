"""One trading cycle at a time, enforced by the database.

A Postgres session-level advisory lock, taken with pg_try_advisory_lock so a
second cycle returns at once instead of queueing behind the first. It is
released when the cycle ends, and the server releases it on its own if the
process dies, so a crashed cycle cannot wedge the next one.

SQLite (local development) has one writer by construction and no advisory
locks, so there it is always granted.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from sqlalchemy import Engine, text

# Arbitrary, fixed, and used for nothing else.
CYCLE_LOCK_KEY = 7_201_160_601


@contextlib.contextmanager
def cycle_lock(engine: Engine) -> Iterator[bool]:
    if engine.dialect.name != "postgresql":
        yield True
        return

    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        held = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": CYCLE_LOCK_KEY}
        ).scalar())
        try:
            yield held
        finally:
            if held:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CYCLE_LOCK_KEY})
    finally:
        conn.close()
