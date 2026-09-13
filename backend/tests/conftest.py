"""
Shared pytest fixtures.

The schema tests are deliberately *not* run against SQLite or a mock: the
point of them is that the SQL in ``migrations/`` and the Core metadata in
``app/data/tables.py`` describe the same real Postgres schema, and a different
engine would prove nothing about either. They therefore need the local
docker-compose ``postgres`` service (or any ``DATABASE_URL``) to be up.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

# backend/ — so `import app` works however pytest was invoked (the project is
# not installed into the venv as a distribution).
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.data import tables
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


@dataclass(frozen=True, slots=True)
class SeededTrip:
    """
    A trip row this fixture put in the database, and the slugs it was given.

    Deliberately its own type rather than the repository's ``TripRecord``: the
    fixture is the *independent* statement of what is in the database, and a
    test that compared the code under test against a value produced by the same
    code under test would assert nothing.
    """

    id: str
    name: str
    start_date: date
    rider_slug: str
    viewer_slug: str


@pytest.fixture
async def seeded_trips(migrated_engine: AsyncEngine) -> AsyncIterator[list[SeededTrip]]:
    """
    Two trips with freshly random slugs, removed again on teardown.

    **Two**, not one, on purpose: a lookup that ignores its slug argument, or
    returns whatever row Postgres hands back first, is indistinguishable from a
    correct one when the table holds a single trip. Access control is the thing
    most worth catching that on — the failure mode is one trip's link opening
    another trip.

    Slugs and ids are ``secrets.token_urlsafe`` values, matching how real slugs
    are issued (spec Section 4) and making every run independent of the last, so
    the suite passes against an empty ``trips`` table and passes again
    immediately afterwards. Nothing here depends on a seeded trip existing.

    The insert is a plain ``INSERT``. It is deliberately **not**
    ``ON CONFLICT ... DO UPDATE`` on a slug column (decision-log entry 3): with
    cryptographically random tokens a collision is not a routine condition to
    absorb, it is a bug signal — a broken RNG or a duplicated insert — and the
    UNIQUE constraint rejecting it loudly is the behaviour we want. Fail, don't
    reconcile.
    """
    seeded = [
        SeededTrip(
            id=f"test-trip-{secrets.token_urlsafe(8)}",
            name=f"Test trip {index}",
            start_date=date(2026, 6, 1 + index),
            rider_slug=secrets.token_urlsafe(16),
            viewer_slug=secrets.token_urlsafe(16),
        )
        for index in range(2)
    ]

    async with migrated_engine.begin() as conn:
        for trip in seeded:
            await conn.execute(
                tables.trips.insert().values(
                    id=trip.id,
                    name=trip.name,
                    rider_slug=trip.rider_slug,
                    viewer_slug=trip.viewer_slug,
                    start_date=trip.start_date,
                )
            )

    try:
        yield seeded
    finally:
        # By id, so a test that (incorrectly) rewrote a slug still gets its row
        # cleaned up rather than leaving one behind to trip a later UNIQUE.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.trips.delete().where(tables.trips.c.id.in_([t.id for t in seeded]))
            )
