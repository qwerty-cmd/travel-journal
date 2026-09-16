"""
``POST /api/trips/{slug}/stops`` — the first wired write endpoint.

**Written from the contract, not from the handler.** Everything asserted here
comes from ``docs/api-contract.md`` (the ``POST /trips/{slug}/stops`` row,
§"Idempotency", §"Access control: 403 and 404 are different answers", §"Error
envelope") and decision-log entry 14. This endpoint sits in two of spec Section
12's three priority categories at once — access control *and* the data integrity
the offline queue depends on — and a test derived from the implementation would
encode the implementation's reading of the contract as the expected answer,
which is exactly the class of mistake entry 14 exists to record.

What the contract promises for this row:

1. **Rider slug only.** A viewer slug is ``403`` / ``FORBIDDEN`` — a real trip,
   a read-only link. A slug nothing resolves to is ``404`` / ``NOT_FOUND``, never
   ``403``: a ``403`` there tells whoever is guessing links that they can
   distinguish "wrong slug" from "right slug, wrong permission", the one signal
   the unguessable-slug model exists to withhold.
2. **No rejected request writes anything.** The status is half the promise; the
   other half is the ``stops`` table. Every negative test below asserts the
   absence of the row **by querying the table directly**, never by re-reading
   through ``GET /trips/{slug}/stops`` — a read path that shares the handler's
   own trip filter would hide a row written under the wrong trip, which is the
   leak this whole endpoint is most able to cause.
3. **The three-way branch on the client-generated id** (§Idempotency, entry 14):
   unseen -> ``201``; already on **this** trip -> ``200`` with the **stored**
   record; on a **different** trip -> ``409`` / ``CONFLICT`` with nothing created
   and nothing about the other trip disclosed. The branch is what makes the
   offline queue safe to retry at all.
4. **The ``409`` discloses nothing.** Its message is rider-facing and returned
   verbatim, so it must carry no value from the conflicting record — not the
   other trip's slug, id or name, not the other stop's name, notes, coordinates
   or timestamp. Asserted against the actual seeded values, so the test fails if
   any of them appears.

Setup follows ``test_stops_list_endpoint.py``: the real app, a real database,
``get_session`` overridden onto this test's engine because the app-level engine
pools asyncpg connections bound to whichever event loop first used them.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededTrip
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.models.stop import StopOut

STOPS_PATH = "/api/trips/{slug}/stops"
OPENAPI_PATH = "/api/trips/{slug}/stops"

# A slug no trip has. Fixed rather than random so a failure says what was asked.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# Exactly the keys `StopOut` promises, written out rather than derived from the
# model: deriving them would make the assertion "the response matches the model"
# when what is under test is the model still matching the written contract.
CONTRACT_KEYS = {"id", "name", "lat", "lng", "locationSource", "arrivedAt", "notes"}

# Every verb, so the access-control sweep below enumerates rather than samples.
ALL_VERBS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]

# The verbs this path really accepts today. `HEAD` is the list route's second,
# schema-excluded registration; `POST` is this endpoint.
PATH_VERBS = {"GET", "HEAD", "POST"}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededStop:
    """
    A stop row this fixture put in the database, in the database's own spelling.

    Snake_case on purpose — ``location_source``, ``arrived_at``, ``trip_id``.
    This is the independent statement of what is in the *table*; a fixture that
    already spelled it the API's way would make the mapping unobservable, and the
    mapping is what a create handler gets wrong.
    """

    id: str
    trip_id: str
    name: str
    lat: float
    lng: float
    location_source: str
    arrived_at: datetime
    notes: str | None


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
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def created_ids(migrated_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """
    Ids this module's *requests* put in the table, deleted again on teardown.

    Rows created through the endpoint are not covered by any seeding fixture's
    cleanup, and ``stops.id`` is a global primary key — a row left behind is a
    row a later run can collide with. Tests append every id they send.
    """
    ids: list[str] = []
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.stops.delete().where(tables.stops.c.id.in_(ids)))


@pytest.fixture
async def existing_stops(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededStop]]:
    """
    One stop already on each seeded trip — the two ids the branch has to tell apart.

    ``-own`` belongs to the **first** trip: a create replaying its id through the
    first trip's rider slug is the ``200`` branch. ``-other`` belongs to the
    **second** trip: the same replay through the *first* trip's slug is the
    ``409`` branch. One fixture rather than two because the whole point is that
    the same request shape lands on different branches depending on a column the
    caller cannot see.

    ``-other``'s field values are deliberately distinctive strings and
    coordinates with no other source in this file, so the ``409`` leak assertion
    can search the raw response text for each of them and a match can only mean
    the handler read the conflicting row. Its ``notes`` is non-null for the same
    reason — a null would make that half of the assertion vacuous.
    """
    first, second = seeded_trips
    prefix = f"test-stop-{secrets.token_urlsafe(8)}"
    base = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)

    seeded = [
        SeededStop(
            id=f"{prefix}-own",
            trip_id=first.id,
            name="Mataranka Thermal Pool",
            lat=-14.9271,
            lng=133.0704,
            location_source="gps",
            arrived_at=base,
            notes="Warm water, cold beer.",
        ),
        SeededStop(
            id=f"{prefix}-other",
            trip_id=second.id,
            name="Wittenoom Gorge Lookout",
            lat=-22.239817,
            lng=118.336142,
            location_source="manual",
            arrived_at=base + timedelta(days=37, hours=3, minutes=17),
            notes="Belongs to another trip and must never surface through this one.",
        ),
    ]

    async with migrated_engine.begin() as conn:
        for stop in seeded:
            await conn.execute(
                tables.stops.insert().values(
                    id=stop.id,
                    trip_id=stop.trip_id,
                    name=stop.name,
                    lat=stop.lat,
                    lng=stop.lng,
                    location_source=stop.location_source,
                    arrived_at=stop.arrived_at,
                    notes=stop.notes,
                )
            )

    try:
        yield seeded
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                tables.stops.delete().where(tables.stops.c.id.in_([s.id for s in seeded]))
            )


def new_payload(**overrides: Any) -> dict[str, Any]:
    """
    A valid ``StopCreate`` body with a fresh client-generated id.

    ``arrivedAt`` carries a **non-UTC** offset (``+09:30``, Northern Territory
    time — where this trip actually is). A ``Z`` value would be indistinguishable
    from a handler that dropped the offset and assumed UTC, which the contract's
    timezone ruling exists to forbid.
    """
    payload: dict[str, Any] = {
        "id": str(uuid4()),
        "name": "Daly Waters Pub",
        "lat": -16.2503,
        "lng": 133.3703,
        "locationSource": "gps",
        "arrivedAt": "2026-06-14T15:15:00+09:30",
        "notes": "Schnitzel, and a bra on the ceiling.",
    }
    payload.update(overrides)
    return payload


async def rows_for_id(engine: AsyncEngine, stop_id: str) -> list[Any]:
    """
    Every ``stops`` row with this id, read **straight from the table**.

    Deliberately not through ``GET /trips/{slug}/stops``: that read applies its
    own ``WHERE trip_id`` filter, so a row written under the wrong trip — the
    precise failure a create endpoint can cause — would be invisible to it and
    a "no row was created" assertion would pass while a row existed. ``stops.id``
    is a global primary key, so this returns every row anywhere in the table.
    """
    async with engine.connect() as conn:
        result = await conn.execute(tables.stops.select().where(tables.stops.c.id == stop_id))
        return list(result.mappings())


async def stop_ids_for_trip(engine: AsyncEngine, trip_id: str) -> set[str]:
    """The ids of every stop on one trip, read straight from the table."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(tables.stops.c.id).where(tables.stops.c.trip_id == trip_id)
        )
        return set(result.scalars())


def parse_envelope(response: Any) -> Any:
    """Validate an error body against ``ErrorEnvelope`` and return its detail."""
    assert response.headers["content-type"] == "application/json", response.headers
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty — it is shown to a rider"
    return envelope.error


# --------------------------------------------------------------------------
# 1. The rider path — an unseen id creates the stop and returns 201
# --------------------------------------------------------------------------


async def test_rider_slug_with_an_unseen_id_returns_201(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """The baseline: the rider's own link creates a stop."""
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text


async def test_the_201_body_is_the_created_stop(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """
    ``StopOut`` for the stop just created — exactly the contract keys, every value
    the one that was sent.

    Whole-object equality rather than spot checks: the mapping is seven keyword
    arguments written by hand and the mistake that shape invites is a crossed
    pair. ``lat``/``lng`` are both plain floats and would swap in silence, so the
    payload gives them values that cannot be confused.

    ``arrivedAt`` is compared as a parsed **instant**, never as a string — see
    the ``arrivedAt`` section at the bottom for why that is the assertion and the
    offset spelling is not.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert {k: v for k, v in body.items() if k != "arrivedAt"} == {
        k: v for k, v in payload.items() if k != "arrivedAt"
    }
    assert datetime.fromisoformat(body["arrivedAt"]) == datetime.fromisoformat(payload["arrivedAt"])
    StopOut.model_validate(body)


async def test_the_created_row_matches_the_request_in_the_table(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    The same claim about the **database**, not the response body.

    A handler that echoed its request back and never inserted would satisfy every
    assertion above. This is the one that says the stop was actually captured —
    which is the entire reason the rider tapped the button.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)
    assert response.status_code == HTTPStatus.CREATED, response.text

    rows = await rows_for_id(migrated_engine, payload["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == payload["name"]
    assert row["lat"] == payload["lat"]
    assert row["lng"] == payload["lng"]
    assert row["location_source"] == payload["locationSource"]
    assert row["notes"] == payload["notes"]
    assert row["arrived_at"] == datetime.fromisoformat(payload["arrivedAt"])


async def test_notes_may_be_omitted_entirely(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ``notes`` is optional, and an absent one is stored as SQL NULL and served as ``null``.

    The field description says omitting the key and sending ``null`` both mean
    "no notes". ``stops.notes`` is nullable, unlike ``bikes.specs``, so the empty
    case is ``null`` and a handler that defaulted it to ``""`` would be inventing
    a value the rider did not write.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    del payload["notes"]
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["notes"] is None
    rows = await rows_for_id(migrated_engine, payload["id"])
    assert rows[0]["notes"] is None


async def test_explicit_null_notes_is_the_same_as_omitting_it(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """Both spellings, because the field description promises they are the same."""
    trip = seeded_trips[0]
    payload = new_payload(notes=None)
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["notes"] is None
    rows = await rows_for_id(migrated_engine, payload["id"])
    assert rows[0]["notes"] is None


async def test_manual_location_source_round_trips(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ``"manual"`` survives the write as well as ``"gps"``.

    Both values, not one: a handler that hardcoded or defaulted the column
    matches on whichever value it happens to emit. This field is the only
    evidence that the GPS-denied capture path ran at all (spec Sections 6, 12),
    and the column carries a CHECK constraint that a third value would trip.
    """
    trip = seeded_trips[0]
    payload = new_payload(locationSource="manual")
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert response.json()["locationSource"] == "manual"
    rows = await rows_for_id(migrated_engine, payload["id"])
    assert rows[0]["location_source"] == "manual"


# --------------------------------------------------------------------------
# 2. Access control — 403, 404, and in both cases no row
# --------------------------------------------------------------------------
# Every negative case asserts the table as well as the status. The status alone
# is not the promise: a 403 that nonetheless wrote the row would be a read-only
# link writing to a journal, and nothing in the response would say so.


async def test_viewer_slug_is_forbidden(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """
    A viewer link cannot create a stop — ``403``, with ``FORBIDDEN`` in the envelope.

    The status and the code are asserted separately: a response that had one
    right and the other wrong would satisfy a combined assertion, and the offline
    queue branches on ``code``, not on the status line.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code == HTTPStatus.FORBIDDEN, response.text
    assert parse_envelope(response).code is ErrorCode.FORBIDDEN


async def test_viewer_slug_creates_no_row(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ...and the stop is **not in the table** — queried directly, not re-read through the API.

    This is the assertion the 403 is actually for. A guard that rejected the
    response after the insert, or one placed below the repository call, returns
    a perfectly correct 403 to a rider whose read-only link just wrote to the
    journal. Only the table can tell those apart.

    The trip's id set is checked too, so a row written under a *different* id
    than the one sent — or under no id this test knows — is still caught.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])
    before = await stop_ids_for_trip(migrated_engine, trip.id)

    response = await client.post(STOPS_PATH.format(slug=trip.viewer_slug), json=payload)
    assert response.status_code == HTTPStatus.FORBIDDEN, response.text

    assert await rows_for_id(migrated_engine, payload["id"]) == []
    assert await stop_ids_for_trip(migrated_engine, trip.id) == before


async def test_unknown_slug_is_not_found(client: AsyncClient, created_ids: list[str]) -> None:
    """A slug no trip has resolves to nothing, and nothing is a 404."""
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert parse_envelope(response).code is ErrorCode.NOT_FOUND


async def test_unknown_slug_is_never_forbidden(client: AsyncClient, created_ids: list[str]) -> None:
    """
    ...and specifically **not** a 403, which is the oracle the slug model rests on.

    Asserted separately from the 404 so the failure names the leak rather than a
    status mismatch: a 403 here tells anyone guessing links that they can
    distinguish "wrong slug" from "right slug, wrong permission".
    """
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert response.status_code != HTTPStatus.FORBIDDEN, response.text


async def test_unknown_slug_creates_no_row(
    client: AsyncClient, migrated_engine: AsyncEngine, created_ids: list[str]
) -> None:
    """A stop with no trip to belong to must not exist anywhere in the table."""
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=UNKNOWN_SLUG), json=payload)
    assert response.status_code == HTTPStatus.NOT_FOUND, response.text

    assert await rows_for_id(migrated_engine, payload["id"]) == []


async def test_no_slug_appears_in_the_403_or_404_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """
    Neither failure body echoes a slug back.

    An error message is the part most likely to be logged or screenshotted, and
    the slug is the only credential this app has — it cannot be rotated without
    re-sending the link to everyone holding it.
    """
    first, second = seeded_trips
    payload = new_payload()
    created_ids.append(payload["id"])

    for slug in (first.viewer_slug, UNKNOWN_SLUG):
        response = await client.post(STOPS_PATH.format(slug=slug), json=payload)

        assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}
        for candidate in (
            first.rider_slug,
            first.viewer_slug,
            second.rider_slug,
            second.viewer_slug,
        ):
            assert candidate not in response.text, f"{candidate!r} leaked into the body"


# --- the guard against an invalid body: which wins is observed, not assumed ---
# The invariant under test is **"no write"**, not which status arrives. A guard
# that runs before body validation answers 403/404; FastAPI validating the body
# first answers 422. Both are defensible readings of the contract, which lists
# 403, 404 and 422 on this row without ordering them — so these tests assert the
# thing the contract does pin (nothing is created, and the response is a
# parseable envelope carrying one of the three) and *record* what was observed
# rather than legislating it.
#
# OBSERVED 2026-09-16, by running these tests against the implementation:
# the **access guard wins**. A viewer slug with a schema-invalid body is 403 /
# FORBIDDEN and an unknown slug with one is 404 / NOT_FOUND — the body is never
# validated, because `require_rider_access` is a route dependency and FastAPI
# resolves dependencies before it binds the request body. That ordering is the
# right way round for the leak: a 422 on an unknown slug would confirm that the
# slug resolved, which is the oracle the 403/404 split exists to withhold.

INVALID_BODIES = {
    "missing-required-fields": {"id": "will-be-replaced"},
    "wrong-types": {
        "id": "will-be-replaced",
        "name": 17,
        "lat": "not a number",
        "lng": None,
        "locationSource": "carrier-pigeon",
        "arrivedAt": "sometime tuesday",
    },
    "naive-arrived-at": {
        "id": "will-be-replaced",
        "name": "No offset on this one",
        "lat": -16.2503,
        "lng": 133.3703,
        "locationSource": "gps",
        "arrivedAt": "2026-06-14T15:15:00",
        "notes": None,
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
    """
    A read-only link plus a malformed body is still a rejection with no row.

    Parametrised over three shapes of invalid body — missing fields, wrong types,
    and the naive ``arrivedAt`` the timezone ruling forbids — because a handler
    that rescued one of them could rescue the others differently.
    """
    trip = seeded_trips[0]
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])
    before = await stop_ids_for_trip(migrated_engine, trip.id)

    response = await client.post(STOPS_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code in {
        HTTPStatus.FORBIDDEN,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    }, response.text
    assert parse_envelope(response).code in {
        ErrorCode.FORBIDDEN,
        ErrorCode.VALIDATION_ERROR,
    }
    assert await rows_for_id(migrated_engine, payload["id"]) == []
    assert await stop_ids_for_trip(migrated_engine, trip.id) == before


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_unknown_slug_with_an_invalid_body_still_writes_nothing(
    client: AsyncClient, migrated_engine: AsyncEngine, created_ids: list[str], case: str
) -> None:
    """The same for a slug nothing resolves to — and still never a 403."""
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=UNKNOWN_SLUG), json=payload)

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
    The observed ordering, pinned now that it has been observed.

    Recorded as its own test rather than folded into the two above, so the two
    claims stay separable: "nothing was written" is contract and must never
    change; "the guard answers first" is the behaviour this implementation
    chose, and if a later change flips it, *this* is the test that says so and
    the ones above keep protecting the invariant regardless.

    It is worth pinning because the ordering is itself an access-control
    property: a 422 on an unknown slug would confirm the slug resolved, which is
    precisely what the 403/404 split withholds.
    """
    trip = seeded_trips[0]
    payload = dict(INVALID_BODIES[case], id=str(uuid4()))
    created_ids.append(payload["id"])

    viewer = await client.post(STOPS_PATH.format(slug=trip.viewer_slug), json=payload)
    unknown = await client.post(STOPS_PATH.format(slug=UNKNOWN_SLUG), json=payload)

    assert viewer.status_code == HTTPStatus.FORBIDDEN, viewer.text
    assert unknown.status_code == HTTPStatus.NOT_FOUND, unknown.text


async def test_a_rider_slug_with_a_naive_arrived_at_is_422_and_writes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    The timezone ruling, enforced at the endpoint and not only in the model.

    ``tests/unit/test_stop_model.py`` proves ``StopCreate`` rejects a naive
    ``arrivedAt``; that says nothing about whether *this route* binds that model.
    A route annotated with a looser type, or one that parsed the body itself,
    would file the stop at whatever hour the session timezone decided — a stop at
    the wrong point in the timeline, which validates, returns a success status
    and is invisible afterwards.

    The rider slug is the one that matters here: with a viewer or unknown slug
    the guard answers first and the body is never reached.
    """
    trip = seeded_trips[0]
    payload = new_payload(arrivedAt="2026-06-14T15:15:00")
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert parse_envelope(response).code is ErrorCode.VALIDATION_ERROR
    assert await rows_for_id(migrated_engine, payload["id"]) == []


# --- every verb on the path, enumerated rather than sampled ---


@pytest.mark.parametrize("verb", sorted(set(ALL_VERBS) - PATH_VERBS))
async def test_every_other_verb_on_the_path_is_405_and_writes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    verb: str,
) -> None:
    """
    ``PUT``/``PATCH``/``DELETE``/``OPTIONS``/``TRACE`` — 405, and no write.

    Enumerated over the full verb list minus the three the path accepts, rather
    than sampling one: the contract lists exactly two write endpoints for stops
    (create, and photos on a stop) and *no* update or delete, so any verb that
    started answering 2xx here would be an endpoint nobody wrote a contract for.
    Sent with the **rider** slug and a valid body on purpose — a verb that leaked
    through to the create handler would then actually write, and this is the test
    that catches it.

    ``Allow`` is asserted as an exact set, the reason its ``/api/health`` sibling
    gives: a substring check survives a hardcoded string, a set that lost
    ``HEAD``, and a set that grew a verb the path does not accept.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.request(verb, STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED, response.text
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
    """
    The same sweep on a read-only link — no verb is a way past the write guard.

    A 405 is the right answer here even though the slug is read-only: the method
    does not exist on this path at all, so there is nothing to be forbidden from.
    What must not happen is a 2xx.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])

    response = await client.request(verb, STOPS_PATH.format(slug=trip.viewer_slug), json=payload)

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED, response.text
    assert await rows_for_id(migrated_engine, payload["id"]) == []


# --- the sibling read route, unchanged ---


async def test_get_still_lists_stops_on_both_slugs(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    Registering ``POST`` on this path must not cost the ``GET`` that was already there.

    Both slugs, because the read row accepts either and the regression worth
    catching is the create endpoint's rider-only guard being attached to the
    router rather than to its own route — which would 403 every viewer on the
    journal itself.
    """
    trip = seeded_trips[0]
    own = next(s for s in existing_stops if s.trip_id == trip.id)

    for slug in (trip.rider_slug, trip.viewer_slug):
        response = await client.get(STOPS_PATH.format(slug=slug))

        assert response.status_code == HTTPStatus.OK, response.text
        assert own.id in {stop["id"] for stop in response.json()}


async def test_head_still_answers_on_both_slugs(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """HEAD is still GET without a body, and an unknown slug is still its 404."""
    trip = seeded_trips[0]

    for slug in (trip.rider_slug, trip.viewer_slug):
        response = await client.head(STOPS_PATH.format(slug=slug))

        assert response.status_code == HTTPStatus.OK, slug
        assert not response.content, "HEAD must not carry a body"

    assert (
        await client.head(STOPS_PATH.format(slug=UNKNOWN_SLUG))
    ).status_code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# 3. The three-way branch — replay, and the id that belongs elsewhere
# --------------------------------------------------------------------------


async def test_replay_of_an_id_on_this_trip_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    The replay branch: an id already on **this** trip is 200, not 201 and not an error.

    This is the whole safety argument for the offline queue retrying writes. The
    connection dropping after the server committed but before the response
    arrived is indistinguishable from a total failure, so the queue retries with
    the id it fixed on the device — and must get a real entity back to reconcile
    against, whichever attempt actually landed.
    """
    trip = seeded_trips[0]
    own = next(s for s in existing_stops if s.trip_id == trip.id)

    response = await client.post(
        STOPS_PATH.format(slug=trip.rider_slug), json=new_payload(id=own.id)
    )

    assert response.status_code == HTTPStatus.OK, response.text


async def test_replay_returns_the_stored_record_not_the_echoed_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    A **different body** on the replay, and the response carries the **stored** values.

    The contract says a replay returns "the existing record" — so the second
    request's field values must be discarded, not merged and not echoed. Sending
    an identical body (the obvious way to write this test) could not tell the
    three apart: every reading produces the same response. Every field is given
    a different value here for that reason.

    Why this matters beyond pedantry: the second request is the *retry*, and on a
    real retry the device is resending a captured body. If the server echoed it,
    a replay and a create would return different things for the same id depending
    on which attempt arrived, and the queue would reconcile its local copy
    against a record the database does not hold.
    """
    trip = seeded_trips[0]
    own = next(s for s in existing_stops if s.trip_id == trip.id)
    different = new_payload(
        id=own.id,
        name="A completely different name",
        lat=12.345678,
        lng=-98.765432,
        locationSource="manual",
        arrivedAt="2027-01-02T03:04:05+00:00",
        notes="Different notes entirely.",
    )

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=different)

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert body["id"] == own.id
    assert body["name"] == own.name
    assert body["lat"] == own.lat
    assert body["lng"] == own.lng
    assert body["locationSource"] == own.location_source
    assert body["notes"] == own.notes
    assert datetime.fromisoformat(body["arrivedAt"]) == own.arrived_at
    # ...and none of the values that were sent came back.
    assert different["name"] not in response.text
    assert different["notes"] not in response.text


async def test_replay_leaves_the_stored_row_unchanged(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_stops: list[SeededStop],
) -> None:
    """
    The same claim about the table: a replay is not an update.

    The response could be built from the stored row while the row was
    nonetheless overwritten first — ``INSERT ... ON CONFLICT DO UPDATE`` returns
    exactly that. Entry 14 is explicit that the replay branch must not overwrite:
    the stored record is the one the rider's first attempt captured, and a retry
    from a device whose local copy drifted must not be allowed to rewrite it.
    """
    trip = seeded_trips[0]
    own = next(s for s in existing_stops if s.trip_id == trip.id)
    different = new_payload(
        id=own.id,
        name="A completely different name",
        lat=12.345678,
        lng=-98.765432,
        locationSource="manual",
        arrivedAt="2027-01-02T03:04:05+00:00",
        notes="Different notes entirely.",
    )

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=different)
    assert response.status_code == HTTPStatus.OK, response.text

    rows = await rows_for_id(migrated_engine, own.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == own.trip_id
    assert row["name"] == own.name
    assert row["lat"] == own.lat
    assert row["lng"] == own.lng
    assert row["location_source"] == own.location_source
    assert row["notes"] == own.notes
    assert row["arrived_at"] == own.arrived_at


async def test_exactly_one_row_exists_after_a_replay(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_stops: list[SeededStop],
) -> None:
    """
    No duplicate — the failure the whole idempotency design exists to prevent.

    Counted across the **whole table** by id (``stops.id`` is a global primary
    key) and the trip's id set is compared before and after, so a duplicate
    filed under this trip and one filed anywhere else are both caught. Three
    attempts, not two: a handler that deduplicated only the *second* request
    would pass a two-attempt test.
    """
    trip = seeded_trips[0]
    own = next(s for s in existing_stops if s.trip_id == trip.id)
    before = await stop_ids_for_trip(migrated_engine, trip.id)

    for _ in range(3):
        response = await client.post(
            STOPS_PATH.format(slug=trip.rider_slug), json=new_payload(id=own.id)
        )
        assert response.status_code == HTTPStatus.OK, response.text

    assert len(await rows_for_id(migrated_engine, own.id)) == 1
    assert await stop_ids_for_trip(migrated_engine, trip.id) == before


async def test_a_fresh_create_replayed_is_a_200_with_the_same_record(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    The offline queue's actual sequence: create, response lost, retry.

    The replay tests above start from a row this file inserted directly. This one
    starts from a row the *endpoint* created, which is the only version that
    proves the 201 and the 200 branches agree about the same stop — a create that
    stored something other than what it returned would show up here and nowhere
    else.

    ``arrivedAt`` is lifted out and compared as an **instant**, not as text.
    Whole-body equality was the first way this was written, and it failed:
    ``2026-06-14T15:15:00+09:30`` from the 201 against ``2026-06-14T05:45:00Z``
    from the 200 — the same moment in two spellings, which is `dev`'s filed
    representation finding. Comparing the raw JSON would have made the offset
    spelling a promise the contract never made and turned that finding into a
    failing test before any consumer exists to care. See section 5's note.
    """
    trip = seeded_trips[0]
    payload = new_payload()
    created_ids.append(payload["id"])
    path = STOPS_PATH.format(slug=trip.rider_slug)

    created = await client.post(path, json=payload)
    assert created.status_code == HTTPStatus.CREATED, created.text

    replayed = await client.post(path, json=payload)

    assert replayed.status_code == HTTPStatus.OK, replayed.text
    created_body, replayed_body = created.json(), replayed.json()
    assert datetime.fromisoformat(replayed_body.pop("arrivedAt")) == datetime.fromisoformat(
        created_body.pop("arrivedAt")
    )
    assert replayed_body == created_body
    assert len(await rows_for_id(migrated_engine, payload["id"])) == 1


async def test_an_id_on_a_different_trip_is_a_409_conflict(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    The third branch: an id that exists under **another** trip is 409 / ``CONFLICT``.

    Not a 200 (that would serve another trip's stop through this trip's slug —
    entry 14's rejected reading 1, and spec Section 12's top failure class), not
    a 422 (the body is well-formed), and not a 500 from a primary-key violation:
    the queue reads ``INTERNAL_ERROR`` as "retry later" and would retry forever a
    request that can never succeed.
    """
    first, _second = seeded_trips
    foreign = next(s for s in existing_stops if s.trip_id != first.id)

    response = await client.post(
        STOPS_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert parse_envelope(response).code is ErrorCode.CONFLICT


async def test_a_cross_trip_id_creates_nothing_and_changes_nothing(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    existing_stops: list[SeededStop],
) -> None:
    """
    Nothing created here, and the other trip's row untouched.

    Both halves. "Nothing created" is the contract's word; the other trip's row
    is what an ``ON CONFLICT DO UPDATE`` or a lookup-by-id-alone would quietly
    rewrite — moving a stop from a trip whose link the caller does not hold.
    """
    first, _second = seeded_trips
    foreign = next(s for s in existing_stops if s.trip_id != first.id)
    before = await stop_ids_for_trip(migrated_engine, first.id)

    response = await client.post(
        STOPS_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )
    assert response.status_code == HTTPStatus.CONFLICT, response.text

    assert await stop_ids_for_trip(migrated_engine, first.id) == before
    rows = await rows_for_id(migrated_engine, foreign.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["trip_id"] == foreign.trip_id
    assert row["name"] == foreign.name
    assert row["lat"] == foreign.lat
    assert row["lng"] == foreign.lng
    assert row["notes"] == foreign.notes
    assert row["arrived_at"] == foreign.arrived_at


async def test_the_409_discloses_nothing_about_the_conflicting_record(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    Not one value from the other trip or the other stop appears in the 409 body.

    Asserted against the **actual seeded values**, so the test fails the moment
    any of them is interpolated into the message — not against a hand-written
    list of forbidden words, which is a list of the leaks already thought of.
    Searched over the raw response text rather than parsed fields, for the same
    reason: the leak to catch is a value somewhere nobody expected.

    A 409 already tells the caller the id exists somewhere. The contract says the
    message must add nothing to that, and the enforcement point is the raise site
    — the handler does not read the conflicting row at all. This is the one place
    in the API where a request about *this* trip is evaluated against a row
    belonging to another, so it is the one place a cross-trip leak can originate
    without any slug being guessed.
    """
    first, second = seeded_trips
    foreign = next(s for s in existing_stops if s.trip_id == second.id)

    response = await client.post(
        STOPS_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    text = response.text
    forbidden = {
        "other trip's name": second.name,
        "other trip's id": second.id,
        "other trip's rider slug": second.rider_slug,
        "other trip's viewer slug": second.viewer_slug,
        "other stop's name": foreign.name,
        "other stop's notes": foreign.notes,
        "other stop's lat": str(foreign.lat),
        "other stop's lng": str(foreign.lng),
        "other stop's arrivedAt": foreign.arrived_at.isoformat(),
        "other stop's arrival date": foreign.arrived_at.date().isoformat(),
    }
    for label, value in forbidden.items():
        assert value, f"the fixture must give the conflicting record a real {label}"
        assert value not in text, f"the 409 leaked the {label}: {value!r}"


async def test_the_409_body_is_the_error_envelope_and_nothing_else(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    The body is ``{"error": {...}}`` — no stop object smuggled alongside it.

    The leak test above searches for values; this one bounds the *shape*, so a
    409 that attached the conflicting ``StopOut`` under some other key fails even
    if its values happened not to match a search string.
    """
    first, _second = seeded_trips
    foreign = next(s for s in existing_stops if s.trip_id != first.id)

    response = await client.post(
        STOPS_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}


async def test_the_same_id_is_a_replay_on_its_own_trip_and_a_conflict_on_the_other(
    client: AsyncClient, seeded_trips: list[SeededTrip], existing_stops: list[SeededStop]
) -> None:
    """
    Both branches from one id, in one test — the ``(trip_id, id)`` lookup, stated directly.

    A handler that looked up by ``id`` alone gives 200 for both. One that always
    conflicted gives 409 for both. Only a parent-scoped lookup gives 200 here and
    409 there, and no single-branch test can distinguish those three.
    """
    first, second = seeded_trips
    foreign = next(s for s in existing_stops if s.trip_id == second.id)

    on_its_own_trip = await client.post(
        STOPS_PATH.format(slug=second.rider_slug), json=new_payload(id=foreign.id)
    )
    on_the_other_trip = await client.post(
        STOPS_PATH.format(slug=first.rider_slug), json=new_payload(id=foreign.id)
    )

    assert on_its_own_trip.status_code == HTTPStatus.OK, on_its_own_trip.text
    assert on_its_own_trip.json()["id"] == foreign.id
    assert on_the_other_trip.status_code == HTTPStatus.CONFLICT, on_the_other_trip.text


# --------------------------------------------------------------------------
# 4. `trip_id` comes from the resolved trip
# --------------------------------------------------------------------------


async def test_the_new_row_is_filed_under_the_resolved_trip(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ``stops.trip_id`` is the **trip's id**, not the slug and not anything from the body.

    The slug is a credential and the id is a key; storing the former in the
    latter's column would survive every response-shaped assertion in this file —
    ``StopOut`` has no ``tripId`` field — and would put a rotatable credential in
    a foreign key. The rider slug is checked explicitly for that reason.

    Asserted on both seeded trips, because a handler that ignored the resolved
    trip and used a first-matching or hardcoded row would be correct for one of
    them by luck.
    """
    for trip in seeded_trips:
        payload = new_payload()
        created_ids.append(payload["id"])

        response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)
        assert response.status_code == HTTPStatus.CREATED, response.text

        row = (await rows_for_id(migrated_engine, payload["id"]))[0]
        assert row["trip_id"] == trip.id
        assert row["trip_id"] != trip.rider_slug
        assert row["trip_id"] != trip.viewer_slug


async def test_a_created_stop_belongs_only_to_the_trip_it_was_created_under(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ...and the other trip's stop set does not grow.

    The same fact from the other side, and the one that would fail if ``trip_id``
    were taken from a body field a caller controls: a stop filed onto a trip
    whose link the caller does not hold is a write leak, the mirror image of the
    read leak the list endpoint guards against.
    """
    first, second = seeded_trips
    payload = new_payload()
    created_ids.append(payload["id"])
    before = await stop_ids_for_trip(migrated_engine, second.id)

    response = await client.post(STOPS_PATH.format(slug=first.rider_slug), json=payload)
    assert response.status_code == HTTPStatus.CREATED, response.text

    assert payload["id"] in await stop_ids_for_trip(migrated_engine, first.id)
    assert await stop_ids_for_trip(migrated_engine, second.id) == before


async def test_a_created_stop_is_visible_through_its_own_trips_list_only(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    End to end: the stop the rider just added appears in their journal and in no other.

    The table assertions above are the precise ones; this is the one that matches
    what a rider would report — "I added a stop and it isn't there", or worse,
    "someone else's trip has my stop in it".
    """
    first, second = seeded_trips
    payload = new_payload()
    created_ids.append(payload["id"])

    created = await client.post(STOPS_PATH.format(slug=first.rider_slug), json=payload)
    assert created.status_code == HTTPStatus.CREATED, created.text

    own = await client.get(STOPS_PATH.format(slug=first.viewer_slug))
    other = await client.get(STOPS_PATH.format(slug=second.rider_slug))

    assert payload["id"] in {stop["id"] for stop in own.json()}
    assert payload["id"] not in {stop["id"] for stop in other.json()}


# --------------------------------------------------------------------------
# 5. `arrivedAt` — the instant, deliberately not its spelling
# --------------------------------------------------------------------------
# `dev` filed a finding: the 201 body echoes the request's `+09:30` offset while
# the 200 replay returns what Postgres hands back (`Z`) — the same instant in two
# spellings. Classified TRIGGERED DEBT, trigger "the first consumer that compares
# `arrivedAt` as a string". That classification is right, and there is nothing to
# test about the *representation* today: no consumer exists, Kubb types the field
# `string` but nothing compares two of them, and a test that pinned one spelling
# would be a test for a consumer that does not exist (finding-triage-gate, Stop
# Condition) — and would also make the representation a promise the contract
# never made.
#
# What the tests below pin is the **instant**, which is true under either
# spelling and is contract today: `arrivedAt` is the moment the rider arrived,
# and both branches describe the same stored row. They fail on the thing that
# would actually hurt — an offset dropped, assumed, or shifted — and constrain
# nothing about how it is written.


async def test_a_non_utc_offset_survives_the_create_as_the_same_instant(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
) -> None:
    """
    ``+09:30`` in, the same instant out — in the body and in the column.

    A half-hour, non-UTC offset on purpose: with a ``Z`` value, a handler that
    dropped the offset and called it UTC is indistinguishable from a correct one,
    and with a whole-hour offset a sign error is easier to miss. The stored column
    is ``TIMESTAMPTZ``, so the comparison is instant-to-instant and says nothing
    about which offset Postgres chooses to render.
    """
    trip = seeded_trips[0]
    sent = datetime(2026, 6, 14, 15, 15, tzinfo=timezone(timedelta(hours=9, minutes=30)))
    payload = new_payload(arrivedAt=sent.isoformat())
    created_ids.append(payload["id"])

    response = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert datetime.fromisoformat(response.json()["arrivedAt"]) == sent
    assert (await rows_for_id(migrated_engine, payload["id"]))[0]["arrived_at"] == sent


async def test_the_201_and_the_200_replay_report_the_same_instant(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """
    The two branches agree on **when**, whatever offset each of them spells it in.

    This is the assertion that stays true whichever way the representation
    finding is eventually resolved, and it fails on the failure that matters: a
    replay that reported a different moment from the create would mean the stored
    instant and the returned one had diverged, and a rider comparing a stop they
    added against the same stop after a sync would see it move.

    Deliberately compared as parsed datetimes and **not** as strings. See the
    section note above: pinning the text would be a test written for a consumer
    that does not exist yet.
    """
    trip = seeded_trips[0]
    sent = datetime(2026, 6, 14, 15, 15, tzinfo=timezone(timedelta(hours=9, minutes=30)))
    payload = new_payload(arrivedAt=sent.isoformat())
    created_ids.append(payload["id"])
    path = STOPS_PATH.format(slug=trip.rider_slug)

    created = await client.post(path, json=payload)
    replayed = await client.post(path, json=payload)

    assert created.status_code == HTTPStatus.CREATED, created.text
    assert replayed.status_code == HTTPStatus.OK, replayed.text
    assert (
        datetime.fromisoformat(created.json()["arrivedAt"])
        == datetime.fromisoformat(replayed.json()["arrivedAt"])
        == sent
    )


async def test_the_created_stop_reads_back_at_the_same_instant_through_the_list(
    client: AsyncClient, seeded_trips: list[SeededTrip], created_ids: list[str]
) -> None:
    """The same instant again through ``GET`` — the endpoint a viewer actually reads."""
    trip = seeded_trips[0]
    sent = datetime(2026, 6, 14, 15, 15, tzinfo=timezone(timedelta(hours=9, minutes=30)))
    payload = new_payload(arrivedAt=sent.isoformat())
    created_ids.append(payload["id"])

    created = await client.post(STOPS_PATH.format(slug=trip.rider_slug), json=payload)
    assert created.status_code == HTTPStatus.CREATED, created.text

    listed = (await client.get(STOPS_PATH.format(slug=trip.viewer_slug))).json()
    stop = next(s for s in listed if s["id"] == payload["id"])

    assert datetime.fromisoformat(stop["arrivedAt"]) == sent


# --------------------------------------------------------------------------
# 6. The OpenAPI document — what Kubb generates the frontend client from
# --------------------------------------------------------------------------


def _openapi() -> dict[str, Any]:
    import app.main

    return app.main.app.openapi()


def test_openapi_declares_the_post_operation_with_stop_create_and_stop_out() -> None:
    """
    The request body is ``StopCreate`` and the success response is ``StopOut``.

    Without both, the generated hook takes and returns ``any`` — and this is the
    endpoint where the client's own types are the only thing standing between a
    rider and a malformed capture, since the contract's timezone rule is
    inexpressible in JSON Schema and cannot be caught client-side at all.
    """
    operation = _openapi()["paths"][OPENAPI_PATH]["post"]

    request = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request["$ref"].rsplit("/", 1)[-1] == "StopCreate"
    success = next(status for status in ("201", "200") if status in operation["responses"])
    schema = operation["responses"][success]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "StopOut"


@pytest.mark.parametrize("status", ["403", "404", "409", "422"])
def test_openapi_declares_every_failure_the_contract_lists(status: str) -> None:
    """
    403, 404, 409 and 422 are declared, each typed as ``ErrorEnvelope``.

    The contract lists exactly these on this row (405 and 500 are framework-level
    and deliberately absent). An undeclared failure is invisible to Kubb, so the
    offline queue — which decides retry-vs-never-retry from ``code`` — would have
    no generated type for the very responses it branches on.
    """
    operation = _openapi()["paths"][OPENAPI_PATH]["post"]

    assert status in operation["responses"], f"the {status} response is not declared"
    schema = operation["responses"][status]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "ErrorEnvelope"


def test_openapi_route_description_follows_the_mandated_format() -> None:
    """
    Context -> How it works -> Related APIs, naming its task.

    Mandatory per CLAUDE.md, and this text is what every downstream reader gets:
    the generated client's doc comment, the Swagger page, and every agent's
    context.
    """
    description = _openapi()["paths"][OPENAPI_PATH]["post"]["description"]

    assert "Context" in description
    assert "How it works" in description
    assert "Related APIs" in description
    assert "t-stops-create-endpoint" in description
    assert len(description) > 400, "a heading-only description is not a description"
