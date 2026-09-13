"""
Shared pytest fixtures.

The schema tests are deliberately *not* run against SQLite or a mock: the
point of them is that the SQL in ``migrations/`` and the Core metadata in
``app/data/tables.py`` describe the same real Postgres schema, and a different
engine would prove nothing about either. They therefore need the local
docker-compose ``postgres`` service (or any ``DATABASE_URL``) to be up.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# backend/ — so `import app` works however pytest was invoked (the project is
# not installed into the venv as a distribution).
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.data.db import normalize_database_url
from app.data.migrate import run_migrations


@pytest.fixture(scope="session")
def database_url() -> str:
    """The configured DATABASE_URL, normalised onto the async driver."""
    return normalize_database_url(get_settings().database_url)


@pytest.fixture
async def migrated_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """
    An engine pointed at a database with every migration applied.

    Applying migrations here rather than in the test keeps the tests honest
    about what they're inspecting: whatever state the database was in, the
    schema under assertion is the one the committed ``.sql`` files produce.
    Migrations are idempotent by version, so running this per test is cheap
    after the first.
    """
    await run_migrations(database_url)
    engine = create_async_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()
