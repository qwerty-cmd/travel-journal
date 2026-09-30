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
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.sessions import ABSOLUTE_LIFETIME, SESSION_COOKIE_NAME, hash_token, new_token
from app.data import tables
from app.data.db import normalize_database_url
from app.data.migrate import run_migrations

# backend/. `import app` itself comes from pyproject's `pythonpath`; this is kept
# for tests that launch a subprocess from the backend directory.
BACKEND_DIR = Path(__file__).resolve().parents[1]

# Every HTTP client a test points at one of our apps looks like the deployed
# SPA calling its own API: HTTPS (so ``Secure`` cookies round-trip) and a
# same-origin ``Origin`` header (so the CSRF check passes as it does for the
# real frontend). Build clients through the two factories below, never by hand
# -- a hand-built ``http://`` client with no ``Origin`` is a cross-site request
# as far as the CSRF middleware is concerned.
TEST_BASE_URL = "https://testserver"
TEST_ORIGIN = "https://testserver"


def _client_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """The default same-origin headers, with any per-call overrides on top."""
    return {"Origin": TEST_ORIGIN, **(headers or {})}


def make_async_client(
    asgi_app: ASGIApp,
    *,
    base_url: str = TEST_BASE_URL,
    headers: Mapping[str, str] | None = None,
    raise_app_exceptions: bool = True,
) -> AsyncClient:
    """
    An ``httpx.AsyncClient`` talking to ``asgi_app`` in-process.

    Use as ``async with make_async_client(app) as client:``. ``base_url`` and
    ``headers`` (merged over the default ``Origin``) are overridable for a test
    that deliberately probes a cross-origin or plain-HTTP request.
    ``raise_app_exceptions=False`` returns the 500 a real client would see
    instead of re-raising the app's exception into the test.
    """
    transport = ASGITransport(app=asgi_app, raise_app_exceptions=raise_app_exceptions)
    return AsyncClient(transport=transport, base_url=base_url, headers=_client_headers(headers))


def make_test_client(
    asgi_app: ASGIApp,
    *,
    base_url: str = TEST_BASE_URL,
    headers: Mapping[str, str] | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    """
    The synchronous counterpart of ``make_async_client``: a Starlette ``TestClient``.

    Same defaults and overrides; ``raise_server_exceptions=False`` returns the
    500 instead of re-raising.
    """
    return TestClient(
        asgi_app,
        base_url=base_url,
        headers=_client_headers(headers),
        raise_server_exceptions=raise_server_exceptions,
    )


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


@dataclass(frozen=True, slots=True)
class SignedInAccount:
    """
    An account this fixture put in the database, with one live session for it.

    ``token`` is the raw cookie value; only its SHA-256 is in ``sessions``, as in
    production. ``headers`` is what a test sends to act as this account: the
    session cookie as a plain ``Cookie`` header, so it can be set on a whole
    client (``make_async_client(app, headers=account.headers)``) or overridden
    on one request (``client.post(..., headers=other.headers)``) without
    touching httpx's cookie jar.
    """

    user_id: str
    display_name: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Cookie": f"{SESSION_COOKIE_NAME}={self.token}"}


async def create_signed_in_account(
    engine: AsyncEngine, *, display_name: str = "Test Rider"
) -> SignedInAccount:
    """
    Insert one user and one fresh session row for it, straight into the tables.

    Straight into the tables rather than through signup, on purpose: this is the
    independent statement of who exists, and a write test should not depend on
    the auth routes working. The password hash is a placeholder no password
    verifies against — nothing here signs in. The session is fresh (``last_used_at``
    now), so it is valid and not due its daily refresh. Remove with
    ``delete_accounts``.
    """
    user_id = str(uuid.uuid4())
    token = new_token()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            tables.users.insert().values(
                id=user_id,
                username=f"t{secrets.token_hex(8)}",
                display_name=display_name,
                password_hash="not-a-password-hash",
            )
        )
        await conn.execute(
            tables.sessions.insert().values(
                token_hash=hash_token(token),
                user_id=user_id,
                created_at=now,
                last_used_at=now,
                absolute_expires_at=now + ABSOLUTE_LIFETIME,
            )
        )
    return SignedInAccount(user_id=user_id, display_name=display_name, token=token)


async def grant_membership(
    engine: AsyncEngine,
    trip_id: str,
    user_id: str,
    *,
    role: str = "rider",
    revoked: bool = False,
) -> None:
    """
    Give ``user_id`` a ``trip_members`` row on ``trip_id``; ``revoked=True`` stores it revoked.

    A revoked row has ``revoked_at`` set and ``revoked_by`` NULL (an operator
    revocation), the shape migration 0003 keeps as history.
    """
    async with engine.begin() as conn:
        await conn.execute(
            tables.trip_members.insert().values(
                id=str(uuid.uuid4()),
                trip_id=trip_id,
                user_id=user_id,
                role=role,
                revoked_at=datetime.now(UTC) if revoked else None,
            )
        )


async def delete_accounts(engine: AsyncEngine, user_ids: list[str]) -> None:
    """
    Remove accounts made by ``create_signed_in_account``, with their memberships and requests.

    ``trip_members.user_id`` and ``join_requests.user_id`` are ``ON DELETE
    RESTRICT``, so those rows go first. Sessions cascade with the user, and
    authorship columns (``created_by``) are ``SET NULL``, so rows a test wrote
    as this account survive for that test's own cleanup.
    """
    async with engine.begin() as conn:
        await conn.execute(
            tables.trip_members.delete().where(tables.trip_members.c.user_id.in_(user_ids))
        )
        await conn.execute(
            tables.join_requests.delete().where(tables.join_requests.c.user_id.in_(user_ids))
        )
        await conn.execute(tables.users.delete().where(tables.users.c.id.in_(user_ids)))


@pytest.fixture
async def rider_session(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[SignedInAccount]:
    """
    A signed-in account with an active ``rider`` membership on **both** seeded trips.

    Since decision-log Entry 29 a slug only locates a trip; a legacy write needs
    a session whose account is an active member (``require_trip_writer``). This
    is the account every write test acts as.

    Both trips, not one, because the existing write tests deliberately write to
    the second trip too (a replayed id under the other trip's slug is the 409
    case). Membership on just one would turn those into 403s and hide the
    behaviour they test. Being a member of one trip but not another is its own
    access-control case, covered in ``test_access_matrix.py`` with its own
    accounts.
    """
    account = await create_signed_in_account(migrated_engine, display_name="Test Rider")
    for trip in seeded_trips:
        await grant_membership(migrated_engine, trip.id, account.user_id)
    try:
        yield account
    finally:
        await delete_accounts(migrated_engine, [account.user_id])


@pytest.fixture
async def non_member_session(migrated_engine: AsyncEngine) -> AsyncIterator[SignedInAccount]:
    """
    A signed-in account with no membership on any trip: the writer gate's ``403`` case.

    What stands in, in the write tests, for the pre-Entry 29 "viewer slug on a
    write": a caller who can locate the trip but may not write to it.
    """
    account = await create_signed_in_account(migrated_engine, display_name="Test Outsider")
    try:
        yield account
    finally:
        await delete_accounts(migrated_engine, [account.user_id])


@pytest.fixture
def s3_bucket() -> None:
    """
    The photo bucket exists in the configured S3 endpoint (local MinIO).

    The one definition every module with a storage leg shares — it was three
    copies (t-s3-bucket-fixture-duplication). ``head_bucket`` first, and
    ``create_bucket`` only on ``ClientError``: that is how boto3 reports "no such
    bucket" (404) or "not yours" (403). Anything else — MinIO not running, bad
    endpoint, bad credentials at the transport level — is a broken environment,
    not a missing bucket, and propagates as a setup error naming the real cause
    instead of a confusing ``create_bucket`` failure behind it.
    """
    from botocore.exceptions import ClientError

    from app.storage.s3_client import BUCKET_NAME, get_s3_client

    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
    except ClientError:
        s3.create_bucket(Bucket=BUCKET_NAME)
