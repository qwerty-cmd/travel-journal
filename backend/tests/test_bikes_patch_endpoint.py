"""
``PATCH /api/trips/{slug}/bikes/{id}`` -- partial update of a bike.

**Written from the contract, not from the handler.** This endpoint sits in
the access-control priority category (rider slug only). Everything asserted
here comes from ``docs/api-contract.md`` (the ``PATCH /trips/{slug}/bikes/{id}``
row and "Access control: 403 and 404 are different answers").

What the contract promises for this row:

1. **Rider slug only.** A viewer slug is ``403`` / ``FORBIDDEN``. A slug
   nothing resolves to is ``404`` / ``NOT_FOUND``, never ``403``.
2. **404 for unknown bike id on this trip.** Also 404 if the bike exists
   but on a *different* trip -- can't patch through the wrong slug.
3. **Partial update.** Only fields present in the request body change.
   Absent fields stay unchanged and are NOT set to null.
4. **Empty body.** Returns 200 with the bike unchanged.
5. **Last-write-wins.** No conflict detection, no ETags.
6. **Response is 200 with the updated ``BikeOut``.**

**Since decision-log Entry 29 (``t-am-write-gate-legacy``).** A slug only
locates the trip; the write gate needs a signed-in, active member. Every request
here acts as the ``rider_session`` fixture (an active rider on both seeded
trips), and the "viewer slug" tests send a ``non_member_session`` instead, so
their ``403`` now comes from the membership gate rather than from the slug. The
assertions are unchanged. A viewer slug with a member's session is a successful
write (contract default 21); ``test_access_matrix.py`` covers that and the rest
of the identity matrix.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import pytest
from conftest import SeededTrip, SignedInAccount, make_async_client
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.bike import BikeOut
from app.models.common import ErrorCode, ErrorEnvelope

PATCH_PATH = "/api/trips/{slug}/bikes/{bike_id}"

UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

CONTRACT_KEYS = {"id", "riderName", "make", "model", "year", "specs"}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededBikeForPatch:
    """A bike row seeded directly into the database, snake_case on purpose."""

    id: str
    trip_id: str
    rider_name: str
    make: str
    model: str
    year: int
    specs: str


@pytest.fixture
async def client(
    migrated_engine: AsyncEngine, rider_session: SignedInAccount
) -> AsyncIterator[AsyncClient]:
    """The real application, talking to the test database."""
    import app.main

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        # Every request acts as `rider_session`, an active member of both seeded
        # trips: since decision-log Entry 29 the slug only locates the trip and the
        # write gate needs a member's session (t-am-write-gate-legacy).
        async with make_async_client(application, headers=rider_session.headers) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def bikes_for_patch(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededBikeForPatch]]:
    """
    One bike on each seeded trip, for patch tests.

    The first bike belongs to seeded_trips[0], the second to seeded_trips[1].
    Field values are all distinct so cross-trip leaks are detectable.
    """
    first, second = seeded_trips
    prefix = f"test-bike-patch-{secrets.token_urlsafe(8)}"

    seeded = [
        SeededBikeForPatch(
            id=f"{prefix}-own",
            trip_id=first.id,
            rider_name="Alice",
            make="Honda",
            model="Africa Twin",
            year=2019,
            specs="Knobblies, 24L tank",
        ),
        SeededBikeForPatch(
            id=f"{prefix}-other",
            trip_id=second.id,
            rider_name="Bartholomew",
            make="Husqvarna",
            model="Norden 901",
            year=2023,
            specs="Belongs to another trip entirely.",
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


async def db_row(engine: AsyncEngine, bike_id: str) -> dict[str, Any] | None:
    """Read a bike row straight from the table, bypassing the API."""
    async with engine.connect() as conn:
        result = await conn.execute(tables.bikes.select().where(tables.bikes.c.id == bike_id))
        row = result.mappings().first()
        return dict(row) if row else None


def parse_envelope(response: Any) -> Any:
    """Validate an error body against ErrorEnvelope and return its detail."""
    assert response.headers["content-type"] == "application/json", response.headers
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty -- it is shown to a rider"
    return envelope.error


def _url(slug: str, bike_id: str) -> str:
    return PATCH_PATH.format(slug=slug, bike_id=bike_id)


# --------------------------------------------------------------------------
# 1. Happy path -- patch one field, only that field changes
# --------------------------------------------------------------------------


async def test_patch_one_field_returns_200(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """Patching a single field returns 200."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"make": "Kawasaki"})

    assert response.status_code == HTTPStatus.OK, response.text


async def test_patch_one_field_updates_only_that_field(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """Only the sent field changes; every other field is returned unchanged."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"make": "Kawasaki"})

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    BikeOut.model_validate(body)
    assert body["make"] == "Kawasaki"
    # Unchanged fields
    assert body["id"] == bike.id
    assert body["riderName"] == bike.rider_name
    assert body["model"] == bike.model
    assert body["year"] == bike.year
    assert body["specs"] == bike.specs


async def test_patch_one_field_updates_only_that_field_in_db(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """The same claim about the database, not the response body."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"make": "Kawasaki"})
    assert response.status_code == HTTPStatus.OK, response.text

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["make"] == "Kawasaki"
    assert row["rider_name"] == bike.rider_name
    assert row["model"] == bike.model
    assert row["year"] == bike.year
    assert row["specs"] == bike.specs


# --------------------------------------------------------------------------
# 2. Patch multiple fields
# --------------------------------------------------------------------------


async def test_patch_multiple_fields(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """Sending multiple fields updates all of them."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]
    patch = {"riderName": "Bob", "make": "KTM", "year": 2025}

    response = await client.patch(_url(trip.rider_slug, bike.id), json=patch)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["riderName"] == "Bob"
    assert body["make"] == "KTM"
    assert body["year"] == 2025
    # Unchanged
    assert body["model"] == bike.model
    assert body["specs"] == bike.specs

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["rider_name"] == "Bob"
    assert row["make"] == "KTM"
    assert row["year"] == 2025


# --------------------------------------------------------------------------
# 3. Empty body -- no fields sent, bike returned as-is
# --------------------------------------------------------------------------


async def test_empty_body_returns_200_with_unchanged_bike(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """An empty patch body is not an error -- the bike comes back unchanged."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={})

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert body["id"] == bike.id
    assert body["riderName"] == bike.rider_name
    assert body["make"] == bike.make
    assert body["model"] == bike.model
    assert body["year"] == bike.year
    assert body["specs"] == bike.specs


async def test_empty_body_does_not_modify_db(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """An empty patch changes nothing in the database."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={})
    assert response.status_code == HTTPStatus.OK, response.text

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["rider_name"] == bike.rider_name
    assert row["make"] == bike.make
    assert row["model"] == bike.model
    assert row["year"] == bike.year
    assert row["specs"] == bike.specs


# --------------------------------------------------------------------------
# 4. Bike not found on this trip -- 404
# --------------------------------------------------------------------------


async def test_unknown_bike_id_is_404(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
) -> None:
    """A bike id that does not exist at all returns 404."""
    trip = seeded_trips[0]

    response = await client.patch(
        _url(trip.rider_slug, "nonexistent-bike-id"), json={"make": "Ducati"}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert parse_envelope(response).code is ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------
# 5. Bike exists on a different trip -- 404
# --------------------------------------------------------------------------


async def test_bike_on_different_trip_is_404(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """
    A bike that exists on trip B cannot be patched through trip A's slug.

    This is the cross-trip isolation rule: lookup is on (trip_id, id).
    """
    first_trip = seeded_trips[0]
    other_bike = bikes_for_patch[1]  # belongs to seeded_trips[1]

    response = await client.patch(
        _url(first_trip.rider_slug, other_bike.id), json={"make": "Ducati"}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert parse_envelope(response).code is ErrorCode.NOT_FOUND


async def test_bike_on_different_trip_is_not_modified(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """A failed cross-trip patch must not modify the other trip's bike."""
    first_trip = seeded_trips[0]
    other_bike = bikes_for_patch[1]

    response = await client.patch(
        _url(first_trip.rider_slug, other_bike.id), json={"make": "Ducati"}
    )
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text

    row = await db_row(migrated_engine, other_bike.id)
    assert row is not None
    assert row["make"] == other_bike.make
    assert row["rider_name"] == other_bike.rider_name


# --------------------------------------------------------------------------
# 6. Viewer slug -- 403
# --------------------------------------------------------------------------


async def test_viewer_slug_is_forbidden(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
    non_member_session: SignedInAccount,
) -> None:
    """A viewer link cannot patch a bike -- 403 FORBIDDEN."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(
        _url(trip.viewer_slug, bike.id), json={"make": "Ducati"}, headers=non_member_session.headers
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert parse_envelope(response).code is ErrorCode.FORBIDDEN


async def test_viewer_slug_does_not_modify_bike(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
    non_member_session: SignedInAccount,
) -> None:
    """A rejected viewer-slug patch must not modify the bike in the database."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(
        _url(trip.viewer_slug, bike.id), json={"make": "Ducati"}, headers=non_member_session.headers
    )
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["make"] == bike.make


# --------------------------------------------------------------------------
# 7. Unknown slug -- 404, never 403
# --------------------------------------------------------------------------


async def test_unknown_slug_is_not_found(
    client: AsyncClient,
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """A slug no trip has is 404."""
    bike = bikes_for_patch[0]

    response = await client.patch(_url(UNKNOWN_SLUG, bike.id), json={"make": "Ducati"})

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert parse_envelope(response).code is ErrorCode.NOT_FOUND


async def test_unknown_slug_is_never_forbidden(
    client: AsyncClient,
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """An unknown slug must never be 403 -- that would be a slug-space oracle."""
    bike = bikes_for_patch[0]

    response = await client.patch(_url(UNKNOWN_SLUG, bike.id), json={"make": "Ducati"})

    assert response.status_code != HTTPStatus.FORBIDDEN, response.text


# --------------------------------------------------------------------------
# 8. Validation: bad year type -- 422
# --------------------------------------------------------------------------


async def test_bad_year_type_is_422(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """A non-integer year is a validation error."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"year": "not a number"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert parse_envelope(response).code is ErrorCode.VALIDATION_ERROR


async def test_bad_year_does_not_modify_bike(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """A validation failure must not modify the bike in the database."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"year": "not a number"})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["year"] == bike.year


# --------------------------------------------------------------------------
# 9. Absent field is NOT set to null
# --------------------------------------------------------------------------


async def test_absent_field_is_not_set_to_null(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """
    Sending only one field must not null out the others.

    This is the core partial-update contract: absent != null.
    """
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(_url(trip.rider_slug, bike.id), json={"specs": "New specs"})
    assert response.status_code == HTTPStatus.OK, response.text

    body = response.json()
    # Every unsent field must retain its original value, not become null
    assert body["riderName"] == bike.rider_name
    assert body["make"] == bike.make
    assert body["model"] == bike.model
    assert body["year"] == bike.year

    row = await db_row(migrated_engine, bike.id)
    assert row is not None
    assert row["rider_name"] == bike.rider_name
    assert row["make"] == bike.make
    assert row["model"] == bike.model
    assert row["year"] == bike.year
    assert row["specs"] == "New specs"


# --------------------------------------------------------------------------
# Access guard ordering: viewer slug + invalid body
# --------------------------------------------------------------------------


async def test_viewer_slug_with_invalid_body_is_still_403(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    bikes_for_patch: list[SeededBikeForPatch],
    non_member_session: SignedInAccount,
) -> None:
    """The access guard runs before body validation -- viewer slug is 403 even with a bad body."""
    trip = seeded_trips[0]
    bike = bikes_for_patch[0]

    response = await client.patch(
        _url(trip.viewer_slug, bike.id),
        json={"year": "not a number"},
        headers=non_member_session.headers,
    )

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text


async def test_unknown_slug_with_invalid_body_is_still_404(
    client: AsyncClient,
    bikes_for_patch: list[SeededBikeForPatch],
) -> None:
    """The access guard runs before body validation -- unknown slug is 404 even with a bad body."""
    bike = bikes_for_patch[0]

    response = await client.patch(_url(UNKNOWN_SLUG, bike.id), json={"year": "not a number"})

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
