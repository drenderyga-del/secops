"""Ordered, tracked SQL migrations with a baseline.

Why not Alembic: the schema is still created by ``Base.metadata.create_all``
on fresh databases, and the two historical SQL files in ``migrations/`` were
applied to production by hand (one of them is a non-idempotent table swap that
must never re-run). Introducing Alembic now would require a synthetic initial
revision and stamping prod manually. This runner gives us the two properties
we actually need — strict ordering and "applied exactly once" tracking — in
~100 lines.

Rules:

- Migration files live in ``migrations/`` and are named ``YYYYMMDD_slug.sql``.
- Files with a date prefix <= BASELINE are recorded as applied but never
  executed: on a fresh DB ``create_all`` already produces the final schema,
  and on the production DB they were applied by hand long ago.
- Files newer than BASELINE are executed in lexicographic order inside a
  transaction each, then recorded in ``schema_migrations``.
- One-off Python data fixes can be registered in PYTHON_MIGRATIONS; they are
  tracked under the same names mechanism so they run exactly once.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Everything shipped before (and including) this date prefix is considered part
# of the baseline schema: present on prod, produced by create_all on fresh DBs.
BASELINE = "20260609"

MIGRATIONS_DIR = Path(os.getenv("MIGRATIONS_DIR", "migrations"))

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    baseline BOOLEAN NOT NULL DEFAULT FALSE
)
"""

# name -> callable(engine). Registered at import time by run_migrations callers.
PYTHON_MIGRATIONS: dict[str, Callable[[Engine], None]] = {}

_SCHEMA_BOOTSTRAP_LOCK_ID = 0x5343414E4F5053


@contextmanager
def schema_bootstrap_lock(engine: Engine) -> Iterator[None]:
    """Serialize create_all and tracked migration setup across app/worker startup."""
    if engine.dialect.name != "postgresql":
        yield
        return

    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _SCHEMA_BOOTSTRAP_LOCK_ID},
        )
        connection.commit()
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _SCHEMA_BOOTSTRAP_LOCK_ID},
            )
            connection.commit()


def _applied_names(engine: Engine) -> set[str]:
    with engine.begin() as conn:
        conn.execute(text(_CREATE_TRACKING_TABLE))
        rows = conn.execute(text("SELECT name FROM schema_migrations")).fetchall()
    return {row[0] for row in rows}


def _record(engine: Engine, name: str, baseline: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migrations (name, baseline) VALUES (:name, :baseline) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"name": name, "baseline": baseline},
        )


def run_migrations(engine: Engine) -> int:
    """Apply pending migrations. Returns the number actually executed."""
    applied = _applied_names(engine)
    executed = 0

    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.is_dir() else []
    for path in sql_files:
        name = path.name
        if name in applied:
            continue
        date_prefix = name.split("_", 1)[0]
        if date_prefix <= BASELINE:
            logger.info("Migration %s is part of the baseline; recording without executing", name)
            _record(engine, name, baseline=True)
            continue
        sql = path.read_text(encoding="utf-8")
        logger.info("Applying migration %s", name)
        # Each file runs in a single transaction. Files that manage their own
        # BEGIN/COMMIT also work: psycopg2 treats them as no-ops inside an
        # already-open transaction block.
        with engine.begin() as conn:
            conn.execute(text(sql))
        _record(engine, name)
        executed += 1

    for name in sorted(PYTHON_MIGRATIONS):
        if name in applied:
            continue
        logger.info("Applying python migration %s", name)
        PYTHON_MIGRATIONS[name](engine)
        _record(engine, name)
        executed += 1

    return executed
