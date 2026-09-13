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


@dataclass(frozen=True, slots=True)
class SeededBike:
    """
    A bike row this fixture put in the database, in the database's own spelling.

    Snake_case on purpose — ``rider_name``, not ``riderName``. This is the
    independent statement of what is in the *table*, and the mapping onto the
    contract's camelCase ``BikeOut.riderName`` is precisely the thing under
    test. A fixture that already spelled it the API's way would make that
    mapping unobservable: a repository that returned the column unchanged and
    one that renamed it correctly would look identical from here.
    """

    id: str
    trip_id: str
    rider_name: str
    make: str
    model: str
    year: int
    specs: str


@pytest.fixture
async def seeded_bikes(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededBike]]:
    """
    Bikes on **both** seeded trips, removed again on teardown.

    Both trips, not one, and that is the point of the fixture rather than a
    detail of it. ``GET /trips/{slug}`` embeds *this trip's* bikes; a query that
    forgot its ``WHERE trip_id = ...`` returns a correct-looking answer for as
    long as every bike in the table happens to belong to the trip being asked
    about. With bikes on the other trip too, the missing filter shows up as
    another trip's bike in this trip's response — which is a cross-trip data
    leak, the same class of failure as one trip's link opening another's.

    The same reasoning rules out any assertion that leans on ``bikes`` being
    empty (decision-log entry 7b: a green test that is green because a condition
    was never created is not evidence). ``bikes`` is empty today only because
    ``POST /trips/{slug}/bikes`` does not exist yet; a test written against that
    accident starts failing in the session that lands it, blamed on whoever did.

    Field values are deliberately all different from one another — no bike
    shares a ``make`` with another's ``rider_name`` — so a repository that
    mapped a column onto the wrong response field cannot produce a body that
    still matches.

    Two bikes share a ``rider_name`` and differ only by id, so the repository's
    ``ORDER BY rider_name, id`` has a tie to break and the resulting order is
    total. Ids are built from one shared random prefix with a numeric suffix, so
    their relative order is the same under any Postgres collation — a test that
    sorted ``secrets.token_urlsafe`` ids in Python and compared against
    Postgres's ordering would be a coin flip on the database's ``lc_collate``.

    Cleanup is by id, matching ``seeded_trips``. The ``trips`` delete would
    cascade these rows away anyway (``bikes.trip_id`` is ``ON DELETE CASCADE``),
    but relying on that would leave any bike a test attached to some *other*
    trip behind forever.
    """
    first, second = seeded_trips
    prefix = f"test-bike-{secrets.token_urlsafe(8)}"

    seeded = [
        # Three on the first trip. Inserted in an order that is neither the
        # ordering the repository promises nor its reverse, so a handler that
        # returned rows in insertion order would not accidentally match.
        SeededBike(
            id=f"{prefix}-01",
            trip_id=first.id,
            rider_name="Zoe",
            make="Honda",
            model="Africa Twin",
            year=2019,
            specs="Knobblies, 24L tank",
        ),
        SeededBike(
            id=f"{prefix}-02",
            trip_id=first.id,
            rider_name="Alex",
            make="Yamaha",
            model="Tenere 700",
            year=2022,
            # Empty specs: the column defaults to '' rather than NULL, and the
            # round trip has to preserve "" and not turn it into null.
            specs="",
        ),
        SeededBike(
            id=f"{prefix}-03",
            trip_id=first.id,
            rider_name="Alex",  # same rider as -02: the id tiebreak
            make="Suzuki",
            model="DR650",
            year=2015,
            specs="Second bike, rally tower",
        ),
        # One on the second trip, and it carries the *same* rider name as a bike
        # on the first. A filter that leaked it into the first trip's response
        # would otherwise be easy to miss among plausible-looking bikes.
        SeededBike(
            id=f"{prefix}-04",
            trip_id=second.id,
            rider_name="Alex",
            make="KTM",
            model="790 Adventure",
            year=2021,
            specs="Other trip entirely",
        ),
    ]

    async with migrated_engine.begin() as conn:
        for bike in seeded:
            await conn.execute(
                tables.bikes.insert().values(
                    id=bike.id,
                    trip_id=bike.trip_id,
                    rider_name=bike.rider_name,
                    make=bike.make,
                    model=bike.model,
                    year=bike.year,
                    specs=bike.specs,
                )
            )

    try:
        yield seeded
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.bikes.delete().where(tables.bikes.c.id.in_([b.id for b in seeded]))
            )
