"""
Migration runner — applies ``backend/migrations/*.sql`` to the database named
by ``DATABASE_URL``.

    cd backend && uv run python -m app.data.migrate

Why plain SQL files and a ledger table rather than a migration framework:
the schema is four tables that the spec fixed up front, and the same runner has
to work against the local docker-compose Postgres and against Neon with nothing
but a connection string. An ordered directory of ``.sql`` files plus a
``schema_migrations`` table is the whole mechanism, it adds no dependency, and
the SQL that ran in prod is the SQL that is committed — not something generated
from Python models at deploy time.

How it works:

1. Ensure ``schema_migrations`` exists (the runner's own bootstrap DDL).
2. Read the applied versions from it.
3. For every ``NNNN_*.sql`` in the migrations directory, sorted by filename,
   that is not already recorded: run the file **whole** and insert its version
   row inside one transaction. Either the whole file lands and is recorded, or
   neither — a half-applied migration can't be left behind for the next run to
   trip over.
4. Report what was applied. A second run applies nothing and says so.

Filename order is the apply order, which is why the numeric prefix is
zero-padded (see ``migrations/README.md``).

Why the file is handed to the driver whole: SQLAlchemy's normal execute path
goes through asyncpg's *extended* query protocol, which accepts exactly one
command per call, so a multi-statement file needs either a client-side SQL
splitter or the driver's simple-query path. Splitting SQL correctly means
hand-parsing string literals, quoted identifiers, dollar-quoted bodies and
nested block comments — a lot of bespoke parsing whose failure mode is a
corrupted migration against production. ``asyncpg.Connection.execute`` with no
bound parameters uses the simple query protocol, which takes the whole file in
one go, and it runs inside the transaction SQLAlchemy already opened on that
same connection — so atomicity and the ledger insert below are unaffected.
Reaching for the driver connection is confined to this module by design;
``data/`` is the only layer allowed to know which database driver is in use
(see CLAUDE.md, "Portability principle").
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.data.db import normalize_database_url

# backend/app/data/migrate.py -> backend/migrations
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# The ledger. Created by the runner itself because it has to exist before any
# migration can be recorded, so it can't live in a migration file.
_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Every ``.sql`` file in the migrations directory, in filename order."""
    return sorted(migrations_dir.glob("*.sql"))


async def run_migrations(
    database_url: str | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """
    Apply every migration that isn't already recorded, and return the list of
    versions applied by this call (empty list = the database was already up to
    date). Each migration runs in its own transaction.
    """
    url = normalize_database_url(database_url or get_settings().database_url)
    engine = create_async_engine(url)
    applied: list[str] = []
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(_LEDGER_DDL)

        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT version FROM schema_migrations"))
            already_applied = {row[0] for row in rows}

        for path in discover_migrations(migrations_dir):
            version = path.stem
            if version in already_applied:
                continue
            sql = path.read_text(encoding="utf-8")
            # engine.begin() = one transaction per migration file. Postgres runs
            # DDL transactionally, so a failure halfway through a file rolls the
            # whole file back and leaves the ledger untouched.
            async with engine.begin() as conn:
                # The whole file, unsplit, through the driver's simple query
                # protocol — see the module docstring. Still inside the
                # transaction opened by engine.begin() above.
                raw_connection = await conn.get_raw_connection()
                await raw_connection.driver_connection.execute(sql)
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                    {"version": version},
                )
            applied.append(version)
    finally:
        await engine.dispose()
    return applied


def main() -> int:
    migrations = discover_migrations()
    if not migrations:
        print(f"No migrations found in {MIGRATIONS_DIR}")
        return 0

    applied = asyncio.run(run_migrations())
    if applied:
        for version in applied:
            print(f"applied {version}")
        print(f"{len(applied)} migration(s) applied.")
    else:
        print(
            f"Nothing to apply - all {len(migrations)} migration(s) already recorded "
            "in schema_migrations."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
