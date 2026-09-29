"""
``POST /api/trips/{slug}/bikes`` — add a bike to a trip.

**Written from the contract, not from the handler.** Everything asserted here
comes from ``docs/api-contract.md`` (the ``POST /trips/{slug}/bikes`` row,
"Idempotency", "Access control: 403 and 404 are different answers", "Error
envelope"). This endpoint sits in the access-control priority category, and
the three-way idempotency branch is the same data-integrity mechanism the
offline queue depends on.

What the contract promises for this row:

1. **Rider slug only.** A viewer slug is ``403`` / ``FORBIDDEN``. A slug
   nothing resolves to is ``404`` / ``NOT_FOUND``, never ``403``.
2. **No rejected request writes anything.** Every negative test asserts the
   absence of the row by querying the ``bikes`` table directly.
3. **Three-way branch on the client-generated id** ("Idempotency"):
   unseen -> ``201``; already on **this** trip -> ``200`` with the **stored**
   record; on a **different** trip -> ``409`` / ``CONFLICT`` with nothing
   created and nothing about the other trip disclosed.
4. **The ``409`` discloses nothing.** Its message carries no value from the
   conflicting record.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededTrip, make_async_client
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.bike import BikeOut
from app.models.common import ErrorCode, ErrorEnvelope

BIKES_PATH = "/api/trips/{slug}/bikes"

# A slug no trip has.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# Exactly the keys ``BikeOut`` promises, written out from the contract.
CONTRACT_KEYS = {"id", "riderName", "make", "model", "year", "specs"}

# Every HTTP verb, so the access-control sweep enumerates rather than samples.
ALL_VERBS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]

# The verbs this path really accepts today. Only POST.
PATH_VERBS = {"POST"}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededBikeForCreate:
    """
    A bike row this fixture put in the database, in the database's own spelling.

    Snake_case on purpose -- ``rider_name``, not ``riderName``. The mapping
    onto the contract's camelCase ``BikeOut.riderName`` is precisely the thing
    under test.
    """

    id: str
    trip_id: str
    rider_name: str
    make: str
    model: str
    year: int
    specs: str


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real application, talking to the test database on this test's event loop."""
    import app.main

    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application = app.main.app
    application.dependency_overrides[get_session] = session_override
    try:
        async with make_async_client(application) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def created_ids(migrated_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """
    Ids this module's *requests* put in the table, deleted again on teardown.

    Rows created through the endpoint are not covered by any seeding fixture's
    cleanup, and ``bikes.id`` is a global primary key.
    """
    ids: list[str] = []
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.bikes.delete().where(tables.bikes.c.id.in_(ids)))


@pytest.fixture
async def existing_bikes(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededBikeForCreate]]:
    """
    One bike already on each seeded trip -- the two ids the branch has to tell apart.

    ``-own`` belongs to the **first** trip: a create replaying its id through the
    first trip's rider slug is the ``200`` branch. ``-other`` belongs to the
    **second** trip: the same replay through the *first* trip's slug is the
    ``409`` branch.

    ``-other``'s field values are deliberately distinctive so the ``409`` leak
    assertion can search the raw response text for each of them.
    """
    first, second = seeded_trips
    prefix = f"test-bike-create-{secrets.token_urlsafe(8)}"

    seeded = [
        SeededBikeForCreate(
            id=f"{prefix}-own",
            trip_id=first.id,
            rider_name="Alice",
            make="Honda",
            model="Africa Twin",
            year=2019,
            specs="Knobblies, 24L tank",
        ),
        SeededBikeForCreate(
            id=f"{prefix}-other",
            trip_id=second.id,
            rider_name="Bartholomew",
            make="Husqvarna",
            model="Norden 901",
            year=2023,
            specs="Belongs to another trip and must never surface through this one.",
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


def new_payload(**overrides: Any) -> dict[str, Any]:
    """A valid ``BikeCreate`` body with a fresh client-generated id."""
    payload: dict[str, Any] = {
        "id": str(uuid4()),
        "riderName": "Zoe",
        "make": "Yamaha",
        "model": "Tenere 700",
        "year": 2022,
        "specs": "Rally tower, bash plate",
    }
    payload.update(overrides)
    return payload


async def rows_for_id(engine: AsyncEngine, bike_id: str) -> list[Any]:
    """
    Every ``bikes`` row with this id, read straight from the table.

    Deliberately not through the API: a read path that applies its own trip
    filter would hide a row written under the wrong trip.
    """
    async with engine.connect() as conn:
        result = await conn.execute(tables.bikes.select().where(tables.bikes.c.id == bike_id))
        return list(result.mappings())


async def bike_ids_for_trip(engine: AsyncEngine, trip_id: str) -> set[str]:
    """The ids of every bike on one trip, read straight from the table."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(tables.bikes.c.id).where(tables.bikes.c.trip_id == trip_id)
        )
        return set(result.scalars())


def parse_envelope(response: Any) -> Any:
    """Validate an error body against ``ErrorEnvelope`` and return its detail."""
    assert response.headers["content-type"] == "application/json", response.headers
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty -- it is shown to a rider"
    return envelope.error


# --------------------------------------------------------------------------
# 1. The rider path -- an unseen id creates the bike and returns 201
# --------------------------------------------------------------------------


async def test_rider_slug_with_an_unseen_id_returns_201(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """The baseline: the rider's own link creates a bike."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text


async def test_the_201_body_is_the_created_bike(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """
    ``BikeOut`` for the bike just created -- exactly the contract keys, every
    value the one that was sent.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert body == payload
    BikeOut.model_validate(body)


async def test_the_created_row_matches_the_request_in_the_table(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """The same claim about the database, not the response body."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=payload)
    assert response.status_code == HTTPStatus.CREATED, response.text

    rows = await rows_for_id(migrated_engine, payload["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["rider_name"] == payload["riderName"]
    assert row["make"] == payload["make"]
    assert row["model"] == payload["model"]
    assert row["year"] == payload["year"]
    assert row["specs"] == payload["specs"]
    assert row["trip_id"] == trip.id


async def test_specs_defaults_to_empty_string_when_omitted(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ``specs`` defaults to ``""`` -- never null. The contract says "not filled in"
    has one representation.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    del payload["specs"]
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["specs"] == ""
    rows = await rows_for_id(migrated_engine, payload["id"])
    assert rows[0]["specs"] == ""


# --------------------------------------------------------------------------
# 2. Access control -- 403, 404, and in both cases no row
# --------------------------------------------------------------------------


async def test_viewer_slug_is_forbidden(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """A viewer link cannot create a bike -- 403, with FORBIDDEN in the envelope."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert parse_envelope(response).code is ErrorCode.FORBIDDEN


async def test_viewer_slug_creates_no_row(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """...and the bike is not in the table -- queried directly, not re-read through the API."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])
    before = await bike_ids_for_trip(migrated_engine, trip.id)

    response = await client.post(BIKES_PATH.format(slug=trip.viewer_slug), json=payload)
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text

    assert await rows_for_id(migrated_engine, payload["id"]) == []
    assert await bike_ids_for_trip(migrated_engine, trip.id) == before


async def test_unknown_slug_is_not_found(client: AsyncClient, created_ids: list[str]) -> None:
    """A slug no trip has resolves to nothing, and nothing is a 404."""
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert parse_envelope(response).code is ErrorCode.NOT_FOUND


async def test_unknown_slug_is_never_forbidden(client: AsyncClient, created_ids: list[str]) -> None:
    """...and specifically not a 403, which is the oracle the slug model rests on."""
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert response.status_code != HTTPStatus.FORBIDDEN, response.text


async def test_unknown_slug_creates_no_row(
    client: AsyncClient, migrated_engine: AsyncEngine, created_ids: list[str]
) -> None:
    """A bike with no trip to belong to must not exist anywhere in the table."""
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=UNKNOWN_SLUG), json=payload)
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text

    assert await rows_for_id(migrated_engine, payload["id"]) == []


async def test_no_slug_appears_in_the_403_or_404_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """Neither failure body echoes a slug back."""
    first, second = seeded_trips
    payload = new_payload()
    created_ids.append(payload["id"])

    for slug in (first.viewer_slug, UNKNOWN_SLUG):
        response = await client.post(BIKES_PATH.format(slug=slug), json=payload)

        assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}
        for candidate in (
            first.rider_slug,
            first.viewer_slug,
            second.rider_slug,
            second.viewer_slug,
        ):
            assert candidate not in response.text, f"{candidate!r} leaked into the body"


# --- invalid body with wrong slug: the guard still wins ---

INVALID_BODIES = {
    "missing-required-fields": {"id": "will-be-replaced"},
    "wrong-types": {
        "id": "will-be-replaced",
        "riderName": 17,
        "make": None,
        "model": [],
        "year": "not a number",
    },
}


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_viewer_slug_with_an_invalid_body_still_writes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    case: str,
) -> None:
    """A read-only link plus a malformed body is still a rejection with no row."""
    trip = seeded_trips[0]
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])
    before = await bike_ids_for_trip(migrated_engine, trip.id)

    response = await client.post(BIKES_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code in {
        HTTPStatus.FORBIDDEN,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }, response.text
    assert parse_envelope(response).code in {
        ErrorCode.FORBIDDEN,
        ErrorCode.VALIDATION_ERROR,
    }
    assert await rows_for_id(migrated_engine, payload["id"]) == []
    assert await bike_ids_for_trip(migrated_engine, trip.id) == before


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_unknown_slug_with_an_invalid_body_still_writes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, created_ids: list[str], case: str
) -> None:
    """The same for a slug nothing resolves to -- and still never a 403."""
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert response.status_code != HTTPStatus.FORBIDDEN, response.text
    assert response.status_code in {
        HTTPStatus.NOT_FOUND,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }, response.text
    assert parse_envelope(response).code in {
        ErrorCode.NOT_FOUND,
        ErrorCode.VALIDATION_ERROR,
    }
    assert await rows_for_id(migrated_engine, payload["id"]) == []


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_the_access_guard_runs_before_body_validation(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str], case: str
) -> None:
    """
    The observed ordering, pinned: the access guard answers before body validation.

    A 422 on an unknown slug would confirm that the slug resolved, which is
    exactly the signal the 403/404 split withholds.
    """
    trip = seeded_trips[0]
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])

    viewer = await client.post(BIKES_PATH.format(slug=trip.viewer_slug), json=payload)
    unknown = await client.post(BIKES_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert viewer.status_code == HTTPStatus.FORBIDDEN, viewer.text
    assert unknown.status_code == HTTPStatus.NOT_FOUND, unknown.text


# --- validation: missing required fields with a rider slug ---


async def test_rider_slug_with_missing_required_fields_is_422(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """Missing required fields on a rider slug is a 422, not a silent default."""
    trip = seeded_trips[0]
    payload = {"id": str(uuid4())}
    created_ids.append(payload["id"])

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert parse_envelope(response).code is ErrorCode.VALIDATION_ERROR
    assert await rows_for_id(migrated_engine, payload["id"]) == []


# --- every verb on the path, enumerated ---


@pytest.mark.parametrize("verb", sorted(set(ALL_VERBS) - PATH_VERBS))
async def test_every_other_verb_on_the_path_is_405_and_writes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    verb: str,
) -> None:
    """
    ``GET``/``HEAD``/``PUT``/``PATCH``/``DELETE``/``OPTIONS``/``TRACE`` -- 405,
    and no write. Sent with the rider slug and a valid body on purpose.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.request(verb, BIKES_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED, response.text
    if verb != "HEAD":
        assert parse_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert {v.strip() for v in response.headers["allow"].split(",")} == PATH_VERBS
    assert await rows_for_id(migrated_engine, payload["id"]) == []


@pytest.mark.parametrize("verb", sorted(set(ALL_VERBS) - PATH_VERBS))
async def test_every_other_verb_is_405_on_the_viewer_slug_too(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    verb: str,
) -> None:
    """The same sweep on a read-only link -- no verb is a way past the write guard."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.request(verb, BIKES_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED, response.text
    assert await rows_for_id(migrated_engine, payload["id"]) == []


# --------------------------------------------------------------------------
# 3. The three-way branch -- replay, and the id that belongs elsewhere
# --------------------------------------------------------------------------


async def test_replay_of_an_id_on_this_trip_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_bikes: list[SeededBikeForCreate]
) -> None:
    """
    The replay branch: an id already on this trip is 200, not 201 and not an error.

    This is the whole safety argument for the offline queue retrying writes.
    """
    trip = seeded_trips[0]
    own = next(b for b in existing_bikes if b.trip_id == trip.id)

    response = await client.post(
        BIKES_PATH.format(slug=trip.rider_slug), json=new_payload(id=own.id)
    )

    assert response.status_code == HTTPStatus.OK, response.text


async def test_replay_returns_the_stored_record_not_the_echoed_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_bikes: list[SeededBikeForCreate]
) -> None:
    """
    A different body on the replay, and the response carries the stored values.

    Every field is given a different value so echoing the request body instead of
    the stored record is caught.
    """
    trip = seeded_trips[0]
    own = next(b for b in existing_bikes if b.trip_id == trip.id)
    different = new_payload(
        id=own.id,
        riderName="A completely different rider",
        make="Ducati",
        model="Multistrada V4",
        year=2025,
        specs="Different specs entirely.",
    )

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=different)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert body["id"] == own.id
    assert body["riderName"] == own.rider_name
    assert body["make"] == own.make
    assert body["model"] == own.model
    assert body["year"] == own.year
    assert body["specs"] == own.specs
    # ...and none of the values that were sent came back.
    assert different["riderName"] not in response.text
    assert different["specs"] not in response.text


async def test_replay_leaves_the_stored_row_unchanged(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_bikes: list[SeededBikeForCreate],
) -> None:
    """The same claim about the table: a replay is not an update."""
    trip = seeded_trips[0]
    own = next(b for b in existing_bikes if b.trip_id == trip.id)
    different = new_payload(
        id=own.id,
        riderName="A completely different rider",
        make="Ducati",
        model="Multistrada V4",
        year=2025,
        specs="Different specs entirely.",
    )

    response = await client.post(BIKES_PATH.format(slug=trip.rider_slug), json=different)
    assert response.status_code == HTTPStatus.OK, response.text

    rows = await rows_for_id(migrated_engine, own.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == own.trip_id
    assert row["rider_name"] == own.rider_name
    assert row["make"] == own.make
    assert row["model"] == own.model
    assert row["year"] == own.year
    assert row["specs"] == own.specs


async def test_exactly_one_row_exists_after_a_replay(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_bikes: list[SeededBikeForCreate],
) -> None:
    """
    No duplicate -- three attempts, not two: a handler that deduplicated only
    the second request would pass a two-attempt test.
    """
    trip = seeded_trips[0]
    own = next(b for b in existing_bikes if b.trip_id == trip.id)
    before = await bike_ids_for_trip(migrated_engine, trip.id)

    for _ in range(3):
        response = await client.post(
            BIKES_PATH.format(slug=trip.rider_slug), json=new_payload(id=own.id)
        )
        assert response.status_code == HTTPStatus.OK, response.text

    assert len(await rows_for_id(migrated_engine, own.id)) == 1
    assert await bike_ids_for_trip(migrated_engine, trip.id) == before


async def test_a_fresh_create_replayed_is_a_200_with_the_same_record(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    The offline queue's actual sequence: create, response lost, retry.

    Starts from a row the endpoint created, proving the 201 and 200 branches
    agree about the same bike.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])
    path = BIKES_PATH.format(slug=trip.rider_slug)

    created = await client.post(path, json=payload)
    assert created.status_code == HTTPStatus.CREATED, created.text

    replayed = await client.post(path, json=payload)

    assert replayed.status_code == HTTPStatus.OK, replayed.text
    assert replayed.json() == created.json()
    assert len(await rows_for_id(migrated_engine, payload["id"])) == 1


async def test_an_id_on_a_different_trip_is_a_409_conflict(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_bikes: list[SeededBikeForCreate]
) -> None:
    """
    The third branch: an id that exists under another trip is 409 / CONFLICT.

    Not a 200 (cross-trip leak), not a 422 (body is well-formed), and not a 500
    from a primary-key violation: the queue reads INTERNAL_ERROR as "retry later"
    and would retry forever.
    """
    first, _second = seeded_trips
    foreign = next(b for b in existing_bikes if b.trip_id != first.id)

    response = await client.post(
        BIKES_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert parse_envelope(response).code is ErrorCode.CONFLICT


async def test_a_cross_trip_id_creates_nothing_and_changes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_bikes: list[SeededBikeForCreate],
) -> None:
    """Nothing created here, and the other trip's row untouched."""
    first, _second = seeded_trips
    foreign = next(b for b in existing_bikes if b.trip_id != first.id)
    before = await bike_ids_for_trip(migrated_engine, first.id)

    response = await client.post(
        BIKES_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )
    assert response.status_code == HTTPStatus.CONFLICT, response.text

    assert await bike_ids_for_trip(migrated_engine, first.id) == before
    rows = await rows_for_id(migrated_engine, foreign.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == foreign.trip_id
    assert row["rider_name"] == foreign.rider_name
    assert row["make"] == foreign.make
    assert row["model"] == foreign.model
    assert row["year"] == foreign.year
    assert row["specs"] == foreign.specs


async def test_the_409_discloses_nothing_about_the_conflicting_record(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_bikes: list[SeededBikeForCreate]
) -> None:
    """
    Not one value from the other trip or the other bike appears in the 409 body.

    Asserted against the actual seeded values so the test fails the moment any
    of them is interpolated into the message.
    """
    first, second = seeded_trips
    foreign = next(b for b in existing_bikes if b.trip_id == second.id)

    response = await client.post(
        BIKES_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    text = response.text
    forbidden = {
        "other trip's name": second.name,
        "other trip's id": second.id,
        "other trip's rider slug": second.rider_slug,
        "other trip's viewer slug": second.viewer_slug,
        "other bike's rider_name": foreign.rider_name,
        "other bike's make": foreign.make,
        "other bike's model": foreign.model,
        "other bike's specs": foreign.specs,
    }
    for label, value in forbidden.items():
        assert value, f"the fixture must give the conflicting record a real {label}"
        assert value not in text, f"the 409 leaked the {label}: {value!r}"


async def test_the_409_body_is_the_error_envelope_and_nothing_else(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_bikes: list[SeededBikeForCreate]
) -> None:
    """The body is ``{"error": {...}}`` -- no bike object smuggled alongside it."""
    first, _second = seeded_trips
    foreign = next(b for b in existing_bikes if b.trip_id != first.id)

    response = await client.post(
        BIKES_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
