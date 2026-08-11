from contextlib import contextmanager
import logging
from typing import Generator, List, Optional

from sqlalchemy import Column, Engine, Table, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def get_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


@contextmanager
def get_session(engine: Engine) -> Generator[Session, None, None]:
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _add_column_ddl(engine: Engine, column: Column) -> Optional[str]:
    """Column fragment for ALTER TABLE ... ADD COLUMN, or None if unsafe.

    A NOT NULL column with no server default cannot be added to a table that
    already has rows — there is nothing to backfill it with. Skip it loudly
    rather than fail the migration or invent values.
    """
    if not column.nullable and column.server_default is None:
        return None
    type_sql = column.type.compile(engine.dialect)
    ddl = f"{column.name} {type_sql}"
    if column.server_default is not None:
        ddl += f" DEFAULT {column.server_default.arg}"
    return ddl


def load_all_models() -> None:
    """Register every table on `Base.metadata` before inspecting it.

    Metadata is populated as a side effect of importing model modules, so a
    process that imports few of them sees few tables. `python -m src.migrate`
    imports almost nothing — without this it would find an empty metadata,
    report "schema up to date", migrate nothing, and leave the pipeline to
    crash on the missing column it was run to add.
    """
    import src.models  # noqa: F401  (import registers the mappers)


def _fresh_inspector(engine: Engine):
    """An inspector that has not cached a previous reflection.

    `inspect(engine)` reuses cached table metadata, so a schema check run after
    a migration in the same process reports the *old* shape. That would make a
    post-migration verification silently pass on a database that never changed.
    """
    inspector = inspect(engine)
    inspector.clear_cache()
    return inspector


def pending_schema_changes(engine: Engine) -> List[str]:
    """What `ensure_schema` WOULD do. Read-only — writes nothing.

    Lets a process detect that it is running against an out-of-date database
    and refuse, instead of migrating as a side effect of starting up.
    """
    load_all_models()
    inspector = _fresh_inspector(engine)
    existing_tables = set(inspector.get_table_names())
    pending: List[str] = []

    table: Table
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            pending.append(f"{table.name} (new table)")
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in present:
                pending.append(f"{table.name}.{column.name}")
    return pending


def ensure_schema(engine: Engine) -> List[str]:
    """Bring an existing database up to the current models. Additive only.

    There is no migration framework in this project, and `create_all` only
    creates missing *tables* — it will not add a new column to the live
    134k-row SQLite file or to Neon. This walks the model metadata and issues
    `ALTER TABLE ... ADD COLUMN` for anything missing.

    Guarantees: never drops, never retypes, never rewrites a row. Idempotent.
    Returns the list of columns added, so a caller can log what changed.

    This WRITES. It must only ever run when someone asked for it — see
    `python -m src.migrate` and the MIGRATE_ON_BOOT flag. Importing an
    application must not move a schema.
    """
    load_all_models()
    Base.metadata.create_all(engine)

    inspector = _fresh_inspector(engine)
    existing_tables = set(inspector.get_table_names())
    added: List[str] = []

    table: Table
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it, so it is already current
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            fragment = _add_column_ddl(engine, column)
            if fragment is None:
                logger.warning(
                    "Skipping migration of %s.%s: NOT NULL with no default "
                    "cannot be added to a populated table",
                    table.name, column.name,
                )
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {fragment}"))
            added.append(f"{table.name}.{column.name}")
            logger.info("Schema migration: added %s.%s", table.name, column.name)

    return added


class SchemaOutOfDate(RuntimeError):
    """The database is behind the models and nobody authorised a migration."""


def verify_or_migrate(engine: Engine, migrate: bool, context: str) -> List[str]:
    """Gate every schema write behind an explicit opt-in.

    `migrate=False` (the default everywhere) means: look, report, change
    nothing. Booting the dashboard must never silently alter the database it
    was pointed at — a read-only tool that migrates production on import is a
    footgun, and the migration may be for code this process is not even running.
    """
    pending = pending_schema_changes(engine)
    if not pending:
        return []

    if not migrate:
        logger.error(
            "DATABASE SCHEMA IS BEHIND THE MODELS — %s did not modify it.\n"
            "  Missing: %s\n"
            "  This process will fail on any query touching the above.\n"
            "  Fix: run  python -m src.migrate\n"
            "  (or set MIGRATE_ON_BOOT=true to migrate at startup — not "
            "recommended for the dashboard).",
            context, ", ".join(pending),
        )
        raise SchemaOutOfDate(f"{len(pending)} pending schema change(s): {pending}")

    logger.info("MIGRATE_ON_BOOT set — applying %d schema change(s)", len(pending))
    return ensure_schema(engine)
