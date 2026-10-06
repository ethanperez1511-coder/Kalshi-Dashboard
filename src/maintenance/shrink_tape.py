"""Shrink the tape table in place, without needing room for a second copy.

After the first rolling-window prune, `orderbook_delta_raw` was a 638 MB file
holding about a third of its former rows. VACUUM FULL would return the space,
but it builds a complete new copy before dropping the old one. At 74% of the cap
the guard correctly refused it, because the worst-case copy did not fit in the
262 MB headroom.

This takes a different route, the one pgcompacttable takes. The live rows sit
at the TAIL of the file (they are the newest), and the pages in front are
empty but reusable. So, a chunk at a time:

  1. MOVE   delete the rows on the highest-numbered pages and re-insert the
            same rows, same ids, in ONE statement and ONE transaction. The
            inserts land in free space lower in the file.
  2. TRIM   plain VACUUM. The tail pages are now empty, and VACUUM truncates
            trailing empty pages off the end of the file.

Peak extra space is one chunk, not one table. Each chunk commits or rolls back
whole: a failure partway leaves every row exactly where it was, and the only
cost is dead tuples inside space the file already has. Indexes are rebuilt
afterwards with REINDEX CONCURRENTLY, one at a time, each only if its estimated
rebuilt size fits.

The dry run is also the measurement the retention ruling needs: true live bytes
by message type, the recording rate, and what fourteen days of tape weighs at
that rate.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import Engine, text

from src.maintenance.retention import (
    DELTA_RETENTION_DAYS,
    MB,
    NEON_CAP_BYTES,
    STORAGE_BUDGET_BYTES,
    database_size_bytes,
    size_status,
)

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "SHRINK-TAPE"
TABLE = "orderbook_delta_raw"

PAGE = 8192
PAGE_USABLE = PAGE - 24
BTREE_FILL = 0.90
CHUNK_ROWS = 20_000
# Never let a chunk take the database past this fraction of the cap.
GUARD_FRACTION = 0.95
# The recorder writes continuously while it runs. Moving rows under it
# contends for the truncation lock, so the shrink waits for a quiet tape.
RECORDER_QUIET_MINUTES = 3
# Stop once this many consecutive rounds fail to shorten the file.
STALL_ROUNDS = 3

_INT_COLUMNS = {"id", "sid", "seq", "ts_ms", "received_at", "price_dollars", "delta_fp"}


@dataclass
class TypeStat:
    msg_type: str
    rows: int
    tuple_bytes: int
    payload_bytes: int


@dataclass
class IndexStat:
    name: str
    columns: List[str]
    size: int
    estimate: int = 0


@dataclass
class TapeMeasure:
    supported: bool
    db_size: int = 0
    heap_size: int = 0
    toast_size: int = 0
    indexes: List[IndexStat] = field(default_factory=list)
    by_type: List[TypeStat] = field(default_factory=list)
    rows_by_day: List[Tuple[dt.date, int]] = field(default_factory=list)
    avg_ticker_len: float = 0.0
    last_received: Optional[dt.datetime] = None

    # --- derived ------------------------------------------------------
    @property
    def rows(self) -> int:
        return sum(t.rows for t in self.by_type)

    @property
    def packed_heap(self) -> int:
        """Heap pages the live rows need when packed: tuple plus line pointer,
        aligned. Toasted payloads are counted at their stored size, which can
        only overstate it."""
        need = sum(t.tuple_bytes for t in self.by_type) + self.rows * 8
        return math.ceil(need / PAGE_USABLE) * PAGE

    @property
    def index_file(self) -> int:
        return sum(i.size for i in self.indexes)

    @property
    def index_estimate(self) -> int:
        return sum(i.estimate for i in self.indexes)

    @property
    def tape_file(self) -> int:
        return self.heap_size + self.toast_size + self.index_file

    @property
    def live_estimate(self) -> int:
        """What the table would weigh rebuilt: the realistic VACUUM FULL copy."""
        return self.packed_heap + self.toast_size + self.index_estimate

    @property
    def bytes_per_row(self) -> float:
        return self.live_estimate / self.rows if self.rows else 0.0

    @property
    def rows_per_day(self) -> float:
        """Median of COMPLETE recorded days: today and the first day of the
        window are partial, and a partial day would understate the rate."""
        full = [n for _, n in self.rows_by_day[1:-1] if n > 0]
        if not full:
            return 0.0
        full.sort()
        return float(full[len(full) // 2])


def _btree_estimate(rows: int, columns: List[str], ticker_len: float) -> int:
    key = 0
    for col in columns:
        if col == "market_ticker" or col not in _INT_COLUMNS:
            key += math.ceil((ticker_len + 1) / 8) * 8
        else:
            key += 8
    per_entry = 8 + key + 4
    return math.ceil(rows * per_entry / (PAGE_USABLE * BTREE_FILL)) * PAGE


def measure(engine: Engine) -> TapeMeasure:
    """One full read of the tape table. Changes nothing."""
    if engine.dialect.name != "postgresql":
        return TapeMeasure(supported=False)

    m = TapeMeasure(supported=True, db_size=database_size_bytes(engine))
    with engine.connect() as conn:
        m.heap_size, total = conn.execute(text(
            f"SELECT pg_relation_size('{TABLE}', 'main'), pg_table_size('{TABLE}')"
        )).one()
        m.toast_size = int(total) - int(m.heap_size)
        m.heap_size = int(m.heap_size)

        for name, size, cols in conn.execute(text(
            "SELECT i.indexrelid::regclass::text, pg_relation_size(i.indexrelid), "
            "       array_agg(a.attname::text ORDER BY k.n) "
            "FROM pg_index i "
            "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, n) "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            f"WHERE i.indrelid = '{TABLE}'::regclass GROUP BY 1, 2 ORDER BY 1"
        )).all():
            m.indexes.append(IndexStat(name, list(cols), int(size)))

        for msg_type, rows, tup, pay, tick in conn.execute(text(
            "SELECT msg_type, count(*), sum(pg_column_size(t.*)), "
            "       sum(coalesce(pg_column_size(payload), 0)), "
            "       avg(octet_length(market_ticker)) "
            f"FROM {TABLE} t GROUP BY msg_type ORDER BY 2 DESC"
        )).all():
            m.by_type.append(TypeStat(msg_type, int(rows), int(tup or 0), int(pay or 0)))
            m.avg_ticker_len = max(m.avg_ticker_len, float(tick or 0))

        m.rows_by_day = [
            (d.date() if isinstance(d, dt.datetime) else d, int(n))
            for d, n in conn.execute(text(
                "SELECT date_trunc('day', received_at AT TIME ZONE 'UTC'), count(*) "
                f"FROM {TABLE} GROUP BY 1 ORDER BY 1"
            )).all()
        ]
        m.last_received = conn.execute(text(f"SELECT max(received_at) FROM {TABLE}")).scalar()

    for idx in m.indexes:
        idx.estimate = _btree_estimate(m.rows, idx.columns, m.avg_ticker_len)
    return m


# --------------------------------------------------------------------------
# Projection: will the file grow, and does the window fit the budget?
# --------------------------------------------------------------------------

def project_live_bytes(m: TapeMeasure, days_ahead: int = 30,
                       window_days: int = DELTA_RETENTION_DAYS,
                       now: Optional[dt.date] = None) -> List[int]:
    """Live tape bytes on each of the next `days_ahead` days, if recording
    continues at the measured rate and retention keeps `window_days`."""
    today = now or dt.datetime.now(dt.timezone.utc).date()
    counts: Dict[dt.date, float] = {d: float(n) for d, n in m.rows_by_day}
    out = []
    for step in range(1, days_ahead + 1):
        day = today + dt.timedelta(days=step)
        counts[day] = m.rows_per_day
        cutoff = day - dt.timedelta(days=window_days)
        live_rows = sum(n for d, n in counts.items() if d > cutoff)
        out.append(int(live_rows * m.bytes_per_row))
    return out


def steady_state_bytes(m: TapeMeasure, window_days: int,
                       full_payload_days: Optional[int] = None) -> int:
    """Tape weight once the window is full. `full_payload_days` models nulling
    DELTA payloads after that many days, as tape.py already does at 14."""
    rows = m.rows_per_day * window_days
    if full_payload_days is None or full_payload_days >= window_days or not m.rows:
        return int(rows * m.bytes_per_row)
    delta = next((t for t in m.by_type if t.msg_type == "delta"), None)
    if delta is None or not delta.rows:
        return int(rows * m.bytes_per_row)
    delta_share = delta.rows / m.rows
    saved_per_delta = delta.payload_bytes / delta.rows
    slim_days = window_days - full_payload_days
    saved = m.rows_per_day * slim_days * delta_share * saved_per_delta
    return int(rows * m.bytes_per_row - saved)


# --------------------------------------------------------------------------
# The shrink itself
# --------------------------------------------------------------------------

@dataclass
class ShrinkResult:
    refused: Optional[str] = None
    rounds: int = 0
    moved: int = 0
    heap_before: int = 0
    heap_after: int = 0
    reindexed: List[str] = field(default_factory=list)
    reindex_skipped: List[str] = field(default_factory=list)
    stopped: str = ""


_MOVE = text(
    f"WITH moved AS ("
    f"  DELETE FROM {TABLE} WHERE ctid = ANY(ARRAY("
    f"    SELECT ctid FROM {TABLE} ORDER BY ctid DESC LIMIT :n"
    f"  )) RETURNING *"
    f") INSERT INTO {TABLE} SELECT * FROM moved"
)


def _heap(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT pg_relation_size('{TABLE}', 'main')")).scalar())


def _vacuum(engine: Engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"VACUUM {TABLE}"))


def guard_refusal(db_size: int, extra: int) -> Optional[str]:
    limit = GUARD_FRACTION * NEON_CAP_BYTES
    if db_size + extra > limit:
        return (
            f"{db_size / MB:.0f} MB + {extra / MB:.0f} MB would exceed "
            f"{GUARD_FRACTION:.0%} of the cap ({limit / MB:.0f} MB)"
        )
    return None


def shrink(
    engine: Engine,
    m: TapeMeasure,
    now: Optional[dt.datetime] = None,
    chunk_rows: int = CHUNK_ROWS,
    max_rounds: Optional[int] = None,
    _after_delete: Optional[Callable[[object], None]] = None,
) -> ShrinkResult:
    """Move tail rows forward a chunk at a time, trimming the file as it
    empties, then rebuild bloated indexes. Every step is atomic or skipped."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result = ShrinkResult(heap_before=_heap(engine))

    if m.last_received is not None:
        last = m.last_received
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        if now - last < dt.timedelta(minutes=RECORDER_QUIET_MINUTES):
            result.refused = (
                f"recorder is writing (last row {last:%H:%M:%S}Z) — dispatch "
                f"between recorder runs"
            )
            result.heap_after = result.heap_before
            return result

    chunk_bytes = int(chunk_rows * m.bytes_per_row) if m.rows else 0
    max_rounds = max_rounds or (2 * math.ceil(m.rows / chunk_rows) + STALL_ROUNDS)
    best = result.heap_before
    stalled = 0

    while result.rounds < max_rounds:
        refusal = guard_refusal(database_size_bytes(engine), chunk_bytes)
        if refusal:
            result.stopped = f"space guard: {refusal}"
            break

        with engine.begin() as conn:
            moved = conn.execute(_MOVE, {"n": chunk_rows}).rowcount or 0
            if _after_delete is not None:
                _after_delete(conn)          # test hook: a failure mid-chunk
        _vacuum(engine)
        result.rounds += 1
        result.moved += moved

        heap = _heap(engine)
        if heap < best:
            best, stalled = heap, 0
        else:
            stalled += 1
        if heap <= m.packed_heap * 1.05:
            result.stopped = "packed: file is within 5% of the live rows"
            break
        if stalled >= STALL_ROUNDS:
            result.stopped = f"no progress for {STALL_ROUNDS} rounds — nothing left to move forward"
            break
    else:
        result.stopped = f"round limit {max_rounds}"

    result.heap_after = _heap(engine)
    _reindex(engine, m, result)
    return result


def _reindex(engine: Engine, m: TapeMeasure, result: ShrinkResult) -> None:
    """REINDEX CONCURRENTLY each index that is meaningfully bloated and whose
    rebuilt size fits under the guard. A failed concurrent rebuild leaves an
    INVALID `_ccnew` index behind, so the cleanup is not optional."""
    for idx in m.indexes:
        if idx.size - idx.estimate < 10 * MB:
            continue
        refusal = guard_refusal(database_size_bytes(engine), idx.estimate)
        if refusal:
            result.reindex_skipped.append(f"{idx.name}: {refusal}")
            continue
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"REINDEX INDEX CONCURRENTLY {idx.name}"))
            result.reindexed.append(idx.name)
        except Exception as exc:
            result.reindex_skipped.append(f"{idx.name}: {exc.__class__.__name__}")
            drop_invalid_indexes(engine)


def drop_invalid_indexes(engine: Engine) -> List[str]:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        names = [r[0] for r in conn.execute(text(
            "SELECT i.indexrelid::regclass::text FROM pg_index i "
            f"WHERE i.indrelid = '{TABLE}'::regclass AND NOT i.indisvalid"
        )).all()]
        for name in names:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
    return names


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def format_report(m: TapeMeasure, result: Optional[ShrinkResult] = None,
                  now: Optional[dt.date] = None) -> str:
    if not m.supported:
        return "SHRINK TAPE: not a Postgres database — nothing to do"

    lines = [
        "SHRINK TAPE " + ("EXECUTED" if result is not None else "DRY RUN / MEASUREMENT"),
        "",
        f"  database            : {size_status(m.db_size)}",
        f"  tape file           : {m.tape_file / MB:.0f} MB "
        f"(heap {m.heap_size / MB:.0f}, toast {m.toast_size / MB:.0f}, "
        f"indexes {m.index_file / MB:.0f})",
        f"  tape live, rebuilt  : {m.live_estimate / MB:.0f} MB "
        f"(heap {m.packed_heap / MB:.0f}, toast ≤{m.toast_size / MB:.0f}, "
        f"indexes ~{m.index_estimate / MB:.0f}) — the realistic VACUUM FULL copy",
        f"  reusable inside file: {(m.tape_file - m.live_estimate) / MB:.0f} MB",
        f"  headroom to cap     : {(NEON_CAP_BYTES - m.db_size) / MB:.0f} MB",
        "",
        f"  {'msg_type':10s} {'rows':>10s} {'MB':>8s} {'payload MB':>11s} {'B/row':>7s}",
    ]
    for t in m.by_type:
        lines.append(
            f"  {t.msg_type:10s} {t.rows:>10,} {t.tuple_bytes / MB:>8.0f} "
            f"{t.payload_bytes / MB:>11.0f} {t.tuple_bytes / max(t.rows, 1):>7.0f}"
        )
    lines.append("")
    lines.append("  indexes (file -> rebuilt estimate):")
    for i in m.indexes:
        lines.append(f"    {i.name:34s} {i.size / MB:>6.0f} -> ~{i.estimate / MB:.0f} MB")
    lines.append("")
    lines.append("  rows by day: " + ", ".join(f"{d:%m-%d} {n:,}" for d, n in m.rows_by_day))
    lines.append(
        f"  recording rate      : {m.rows_per_day:,.0f} rows/day (median of complete "
        f"days) x {m.bytes_per_row:.0f} B/row incl. indexes"
    )

    other = m.db_size - m.tape_file
    lines.append("")
    lines.append(f"  STEADY STATE (everything else in the DB: {other / MB:.0f} MB file)")
    for label, window, slim in (
        (f"{DELTA_RETENTION_DAYS}d window, full payloads (current)", DELTA_RETENTION_DAYS, None),
        (f"{DELTA_RETENTION_DAYS}d window, delta payloads nulled after 2d", DELTA_RETENTION_DAYS, 2),
        ("10d window, full payloads", 10, None),
        ("7d window, full payloads", 7, None),
    ):
        tape = steady_state_bytes(m, window, slim)
        total = tape + other
        verdict = "fits budget" if total <= STORAGE_BUDGET_BYTES else "OVER budget"
        lines.append(f"    {label:44s} tape {tape / MB:>5.0f} MB  total ~{total / MB:>5.0f} MB  {verdict}")

    future = project_live_bytes(m, now=now)
    peak = max(future) if future else 0
    if peak <= m.tape_file:
        lines.append(
            f"\n  FILE GROWTH: none expected in 30 days — projected live peak "
            f"{peak / MB:.0f} MB fits inside the {m.tape_file / MB:.0f} MB file"
        )
    else:
        day = next(i for i, b in enumerate(future, 1) if b > m.tape_file)
        lines.append(
            f"\n  FILE GROWTH: live tape fills the file in ~{day} days and the file "
            f"grows to ~{peak / MB:.0f} MB (+{(peak - m.tape_file) / MB:.0f} MB), then "
            f"holds there. Expected after a shrink; a risk only if that steady "
            f"state is near the cap"
        )

    if result is not None:
        lines += ["", "  RESULT"]
        if result.refused:
            lines.append(f"    REFUSED: {result.refused}")
        lines += [
            f"    rounds {result.rounds}, rows moved {result.moved:,}",
            f"    heap {result.heap_before / MB:.0f} -> {result.heap_after / MB:.0f} MB",
            f"    stopped: {result.stopped or '-'}",
            f"    reindexed: {', '.join(result.reindexed) or 'none'}",
        ]
        for skip in result.reindex_skipped:
            lines.append(f"    reindex skipped: {skip}")
    else:
        lines += [
            "",
            f"  To execute: re-run with confirm={CONFIRM_TOKEN}. Each chunk of "
            f"{CHUNK_ROWS:,} rows is one transaction;",
            "  a killed run keeps every completed chunk and loses nothing. Dispatch "
            "between recorder runs.",
        ]
    return "\n".join(lines)
