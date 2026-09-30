"""
``GET /api/trips/{slug}`` — the first real endpoint, and the first time the
contract in ``docs/api-contract.md`` is asserted against a live response.

What is under test here is not "does a query work". It is the four separate
promises this one response makes, each of which fails independently:

1. **The body shape.** Exactly ``{id, name, startDate, bikes, access}``,
   camelCase on the wire while the columns are snake_case. The generated
   frontend client (Kubb, from this app's OpenAPI spec) is typed off that shape,
   so a renamed or extra key is a compile error three sessions later in a
   different repository.
2. **Which bikes.** ``TripOut.bikes`` is *this* trip's bikes. A missing
   ``WHERE trip_id`` is not a cosmetic bug — it is one trip's data appearing
   under another trip's link, which is the same class of failure as one trip's
   slug opening another trip's journal.
3. **``access``.** It is derived by the read gate from the caller's membership
   (since decision-log Entry 29; ``rider`` iff an active member, whichever slug)
   and passed through untouched. It drives whether the frontend renders write UI, so
   an ``access`` stuck at ``rider`` shows a read-only guest buttons that always
   fail, while the API's own enforcement stays perfectly correct — the two are
   different mechanisms and only one of them is tested by the 403 tests in
   ``test_slug_access.py``.
4. **The slug never comes back.** The slug *is* the credential; there is no
   password to rotate and no account to lock. Asserted as a substring search
   over the raw response text, because a leak into a field nobody expected is
   still a leak.

Deliberate choices about the setup:

- **The real app, not a probe app.** ``test_slug_access.py`` and
  ``test_error_envelope.py`` both build throwaway apps because they predate any
  registered endpoint. This one does not: the thing under test is the handler in
  ``app/api/routes/trips.py`` as mounted at ``/api/trips/{slug}``, including its
  OpenAPI description, so a copy of it in this file would assert nothing about
  what a client receives.
- **``get_session`` is overridden onto this test's engine.** Not because the
  real one is wrong, but because the app-level engine is built at import time
  and pools asyncpg connections bound to whichever event loop first used them
  (see ``app/data/db.py``). ``test_route_declares_require_trip_access`` checks
  separately that the route really does go through the shared dependency, so the
  override cannot be silently pointed at something the app does not use.
- **A real database.** The endpoint is a query plus a mapping; a stubbed
  repository would test the mapping and nothing about whether the right rows
  come back.
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import AsyncIterator
from datetime import date
from http import HTTPStatus
from typing import Any, get_args

import pytest
from conftest import SeededBike, SeededTrip, SignedInAccount, make_async_client
from fastapi import params
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.security import require_trip_access, require_trip_writer
from app.data import tables
from app.data.db import get_session
from app.data.repositories.bikes import list_by_trip
from app.models.bike import BikeOut
from app.models.common import ErrorCode, ErrorEnvelope
from app.models.trip import Access, TripOut

# The route as a client sees it: the router's own prefix, under /api.
TRIP_PATH = "/api/trips/{slug}"

# The path template as FastAPI renders it into the OpenAPI spec.
OPENAPI_PATH = "/api/trips/{slug}"

# A slug no trip has. Fixed rather than random so a failure message says plainly
# what was asked for.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# Exactly the keys `TripOut` promises. Written out as a literal rather than
# derived from the model, on purpose: deriving it from `TripOut.model_fields`
# would make the assertion "the response matches the model" and this test is
# meant to catch the model itself drifting away from the written contract.
CONTRACT_KEYS = {"id", "name", "startDate", "bikes", "access"}

BIKE_CONTRACT_KEYS = {"id", "riderName", "make", "model", "year", "specs"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


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
async def db_session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A bare session for the repository tests, which do not go through HTTP."""
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session


@pytest.fixture
async def trip_without_bikes(migrated_engine: AsyncEngine) -> AsyncIterator[SeededTrip]:
    """
    A third trip that has no bikes, created *alongside* trips that do.

    The empty-``bikes`` case could be tested by simply not asking for
    ``seeded_bikes`` — but then it would pass against a table with no bikes in
    it at all, which is decision-log entry 7b's failure mode exactly: a test
    that is green because a condition was never created. Used together with
    ``seeded_bikes``, this trip proves the endpoint returns ``[]`` for a trip
    with no bikes *while other trips' bikes exist in the table*, which is the
    only version of the claim that will still be true after
    ``POST /trips/{slug}/bikes`` lands.
    """
    trip = SeededTrip(
        id=f"test-trip-{secrets.token_urlsafe(8)}",
        name="Trip with no bikes",
        start_date=date(2026, 7, 4),
        rider_slug=secrets.token_urlsafe(16),
        viewer_slug=secrets.token_urlsafe(16),
    )

    async with migrated_engine.begin() as conn:
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
        yield trip
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.trips.delete().where(tables.trips.c.id == trip.id))


def expected_bike_body(bike: SeededBike) -> dict[str, Any]:
    """
    The JSON one seeded bike must serialise to — written from the *database* row.

    Built here rather than by calling the repository, so the snake_case ->
    camelCase mapping is stated independently of the code that performs it.
    """
    return {
        "id": bike.id,
        "riderName": bike.rider_name,
        "make": bike.make,
        "model": bike.model,
        "year": bike.year,
        "specs": bike.specs,
    }


def bikes_by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    The response's bikes keyed by id — never indexed positionally.

    The repository orders its rows so the response is stable between identical
    requests, but that order is explicitly **not** a contract guarantee (see
    ``list_by_trip``). Tests that matched by position would quietly encode a
    promise the API does not make.
    """
    return {bike["id"]: bike for bike in body["bikes"]}


# --------------------------------------------------------------------------
# 1. The body shape — exactly the contract's keys, in camelCase
# --------------------------------------------------------------------------


async def test_rider_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """The baseline: the rider's own link opens their trip."""
    trip = seeded_trips[0]

    response = await client.get(TRIP_PATH.format(slug=trip.rider_slug))

    assert response.status_code == HTTPStatus.OK


async def test_body_has_exactly_the_contract_keys(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    ``{id, name, startDate, bikes, access}`` — no more, no fewer.

    Both halves matter. A *missing* key breaks the generated client's type. An
    *extra* one is worse than untidy here: the only extra values in scope are
    the two slugs the record carries, and a response that grew fields by
    accident is how one of them reaches a log or a screenshot.
    """
    trip = seeded_trips[0]

    response = await client.get(TRIP_PATH.format(slug=trip.rider_slug))

    assert response.status_code == HTTPStatus.OK
    assert set(response.json().keys()) == CONTRACT_KEYS


async def test_body_fields_carry_the_trips_own_values(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """Each key holds that trip's value — asserted against the fixture, not the repository."""
    trip = seeded_trips[1]

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert body["id"] == trip.id
    assert body["name"] == trip.name


async def test_start_date_is_an_iso_date_string(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    ``startDate`` is ``YYYY-MM-DD`` — a calendar day, not a timestamp.

    Asserted as a string *and* parsed back, because a field serialised as a
    datetime ("2026-06-01T00:00:00") would still round-trip through
    ``date.fromisoformat`` on some inputs and would still look plausible in a
    body, while shifting the journal's start day for any viewer in another
    timezone.
    """
    trip = seeded_trips[0]

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert isinstance(body["startDate"], str)
    assert body["startDate"] == trip.start_date.isoformat()
    assert date.fromisoformat(body["startDate"]) == trip.start_date


async def test_body_validates_against_the_contract_model(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    The body parses as ``TripOut`` — the shape the generated client expects.

    Complements the key-set assertion rather than repeating it: this one checks
    the *types* (``bikes`` a list of bike objects, ``access`` one of two literal
    values) that a key comparison cannot see.
    """
    trip = seeded_trips[0]

    response = await client.get(TRIP_PATH.format(slug=trip.rider_slug))

    parsed = TripOut.model_validate(response.json())
    assert parsed.id == trip.id


# --------------------------------------------------------------------------
# 2. Either slug reads, and the payload is the same trip
# --------------------------------------------------------------------------


async def test_viewer_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    A viewer link opens the trip. Read-only is not locked out.

    This is the assertion that fails if the route is ever switched to the write
    guard: every viewer in existence would get a 403 on the app's first request,
    and the only report would be "the link you sent me doesn't work".
    """
    trip = seeded_trips[0]

    response = await client.get(TRIP_PATH.format(slug=trip.viewer_slug))

    assert response.status_code == HTTPStatus.OK


async def test_viewer_slug_is_not_forbidden(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """Stated as its own negative: a 403 here is the specific regression to catch."""
    trip = seeded_trips[0]

    response = await client.get(TRIP_PATH.format(slug=trip.viewer_slug))

    assert response.status_code != HTTPStatus.FORBIDDEN


async def test_both_slugs_return_an_identical_trip_payload(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    Same trip, same bikes, same everything except ``access``.

    ``access`` is the *only* thing the two links may differ on. If a viewer saw
    a different name, a shorter bike list or a different id, the read-only link
    would be showing a different journal from the one the rider shared.
    """
    trip = seeded_trips[0]

    rider = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()
    viewer = (await client.get(TRIP_PATH.format(slug=trip.viewer_slug))).json()

    assert {k: v for k, v in rider.items() if k != "access"} == {
        k: v for k, v in viewer.items() if k != "access"
    }


# --------------------------------------------------------------------------
# 3. `access` comes from the dependency, per request
# --------------------------------------------------------------------------


# Since decision-log Entry 29 (t-am-write-gate-legacy) `access` follows the
# caller's membership, not the slug: either slug only locates the trip (contract
# default 21). The two tests below were "the rider's link reports rider" and
# "the two links differ"; they are now the membership versions of the same
# checks. The others in this section ask with no session, where both links
# report `viewer`, so they are unchanged.


async def test_rider_slug_reports_rider_access(
    client: AsyncClient, seeded_trips: list[SeededTrip], rider_session: SignedInAccount
) -> None:
    """An active member reports ``rider`` — through either link (contract default 21)."""
    trip = seeded_trips[0]

    for slug in (trip.rider_slug, trip.viewer_slug):
        body = (await client.get(TRIP_PATH.format(slug=slug), headers=rider_session.headers)).json()

        assert body["access"] == Access.RIDER.value


async def test_viewer_slug_reports_viewer_access(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    The viewer's link reports ``viewer`` — the half a hardcoded value would break.

    Nothing else in the response differs between the two links, so this single
    field is the entire signal the frontend has for whether to render Add stop,
    photo upload and bike editing at all.
    """
    trip = seeded_trips[0]

    body = (await client.get(TRIP_PATH.format(slug=trip.viewer_slug))).json()

    assert body["access"] == Access.VIEWER.value


async def test_access_differs_between_a_member_and_a_non_member_on_one_link(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    rider_session: SignedInAccount,
    non_member_session: SignedInAccount,
) -> None:
    """
    One trip, one link, two callers, two different answers (contract default 21).

    Asserted as a difference as well as by value: a handler that read ``access``
    off the stored row or off the slug instead of off the caller's membership
    would have to give the same answer to both, and this is the shape of that
    bug. Checked on both links, so neither slug decides it.
    """
    trip = seeded_trips[0]

    for slug in (trip.rider_slug, trip.viewer_slug):
        member = (
            await client.get(TRIP_PATH.format(slug=slug), headers=rider_session.headers)
        ).json()
        stranger = (
            await client.get(TRIP_PATH.format(slug=slug), headers=non_member_session.headers)
        ).json()

        assert member["access"] != stranger["access"]


async def test_second_trips_viewer_slug_reports_viewer_access(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """The same with two trips in the table — the permission is read off the right row."""
    _, second = seeded_trips

    body = (await client.get(TRIP_PATH.format(slug=second.viewer_slug))).json()

    assert body["id"] == second.id
    assert body["access"] == Access.VIEWER.value


# --------------------------------------------------------------------------
# 4. An unknown slug — 404, in the envelope
# --------------------------------------------------------------------------


async def test_unknown_slug_is_not_found(client: AsyncClient) -> None:
    """A slug no trip has resolves to nothing, and nothing is a 404."""
    response = await client.get(TRIP_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_unknown_slug_body_is_a_valid_error_envelope(client: AsyncClient) -> None:
    """
    The failure body parses as ``ErrorEnvelope``, with a non-empty message.

    Parsed through the contract model rather than poked at as a dict, so what is
    asserted is "the shape the generated client is typed for" and not "it
    happens to contain the keys I looked for".
    """
    response = await client.get(TRIP_PATH.format(slug=UNKNOWN_SLUG))

    assert response.headers["content-type"] == "application/json"
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message


async def test_unknown_slug_reports_not_found_code(client: AsyncClient) -> None:
    """
    ...with ``code == "NOT_FOUND"``, asserted separately from the status.

    The offline queue branches on ``code``, not on the status line, to decide
    whether a request can ever succeed — so the code is its own assertion.
    """
    response = await client.get(TRIP_PATH.format(slug=UNKNOWN_SLUG))

    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code is ErrorCode.NOT_FOUND


async def test_unknown_slug_does_not_return_a_trip_body(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """No trip's id reaches a response for a slug that resolves to no trip."""
    response = await client.get(TRIP_PATH.format(slug=UNKNOWN_SLUG))

    for trip in seeded_trips:
        assert trip.id not in response.text


# --------------------------------------------------------------------------
# 5. `bikes` — this trip's, all of them, correctly mapped
# --------------------------------------------------------------------------


async def test_bikes_are_exactly_this_trips_bikes(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    The id set matches the trip's own bikes — nothing missing, nothing borrowed.

    The second trip's bike is the one that matters: ``seeded_bikes`` gives it the
    *same rider name* as a bike on this trip, so a query that dropped its
    ``WHERE trip_id`` would return a body that still looks entirely plausible to
    a human reading it. Only the id set catches it.
    """
    trip = seeded_trips[0]
    expected = {b.id for b in seeded_bikes if b.trip_id == trip.id}
    foreign = {b.id for b in seeded_bikes if b.trip_id != trip.id}
    assert expected and foreign, "the fixture must seed bikes on both trips for this to bite"

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert set(bikes_by_id(body)) == expected


async def test_no_other_trips_bike_appears_in_the_response(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    The same fact stated as the leak it would be, and asserted over the raw text.

    A cross-trip bike is one trip's data served under another trip's link. Named
    separately from the id-set test because this is the failure a reviewer
    should find by name when it happens.
    """
    first, second = seeded_trips
    foreign = [b for b in seeded_bikes if b.trip_id == second.id]
    assert foreign, "the fixture must seed a bike on the other trip"

    response = await client.get(TRIP_PATH.format(slug=first.rider_slug))

    for bike in foreign:
        assert bike.id not in response.text
        assert bike.make not in response.text


async def test_each_bike_carries_every_field_from_its_own_row(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    Every field of every bike equals its own column — including ``rider_name`` -> ``riderName``.

    Whole-object equality rather than field-by-field spot checks: the mapping is
    six keyword arguments written by hand, and the mistake that shape invites is
    passing the wrong column to one of them (``riderName=row.make``). The
    fixture gives every bike distinct values for every field precisely so that a
    crossed pair cannot produce a body that still matches.
    """
    trip = seeded_trips[0]
    expected = {b.id: expected_bike_body(b) for b in seeded_bikes if b.trip_id == trip.id}

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert bikes_by_id(body) == expected


async def test_bike_objects_have_exactly_the_contract_keys(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """``BikeOut`` is ``{id, riderName, make, model, year, specs}`` and nothing else."""
    trip = seeded_trips[0]

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert body["bikes"], "this trip must have bikes for the assertion to mean anything"
    for bike in body["bikes"]:
        assert set(bike.keys()) == BIKE_CONTRACT_KEYS


async def test_empty_specs_stays_an_empty_string(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    A bike with no specs written yet serialises ``""``, never ``null``.

    The column is ``NOT NULL DEFAULT ''`` so that "nothing written yet" has one
    representation; a ``null`` on the wire would give the frontend a second one
    to handle.
    """
    trip = seeded_trips[0]
    blank = next(b for b in seeded_bikes if b.trip_id == trip.id and b.specs == "")

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert bikes_by_id(body)[blank.id]["specs"] == ""


async def test_a_trip_with_no_bikes_returns_an_empty_list(
    client: AsyncClient,
    trip_without_bikes: SeededTrip,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
) -> None:
    """
    200 with ``"bikes": []`` — not a 404, not ``null``, not a missing key.

    A trip with no bikes is an ordinary trip: every trip is one until the rider
    adds the first bike. Run with ``seeded_bikes`` active, so the table is *not*
    empty and the empty list is a real answer about this trip rather than an
    accident of an unpopulated table (decision-log entry 7b).
    """
    response = await client.get(TRIP_PATH.format(slug=trip_without_bikes.rider_slug))

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["id"] == trip_without_bikes.id
    assert body["bikes"] == []
    assert body["bikes"] is not None


async def test_bikes_key_is_present_even_when_empty(
    client: AsyncClient,
    trip_without_bikes: SeededTrip,
    seeded_bikes: list[SeededBike],
) -> None:
    """The key set is the same whether or not the trip has bikes."""
    body = (await client.get(TRIP_PATH.format(slug=trip_without_bikes.rider_slug))).json()

    assert set(body.keys()) == CONTRACT_KEYS


# --------------------------------------------------------------------------
# 6. Order — stable between requests, and deliberately not a contract promise
# --------------------------------------------------------------------------
# `list_by_trip` orders by `rider_name, id` so that two identical requests
# produce identical JSON. That is a *stability* property, not an API guarantee:
# the contract says nothing about the order of `bikes`, and nothing outside
# the tests in this section may depend on a bike's position — every other test
# in this file looks bikes up by id.


async def test_bike_order_is_stable_across_identical_requests(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    The same request twice gives byte-identical bodies.

    Without an ``ORDER BY``, Postgres is free to return rows in any order, and a
    body that reshuffles between calls turns every cached or diffed copy of it
    into noise.
    """
    trip = seeded_trips[0]
    path = TRIP_PATH.format(slug=trip.rider_slug)

    first = await client.get(path)
    second = await client.get(path)

    assert first.text == second.text


async def database_order(engine: AsyncEngine, bike_ids: list[str]) -> list[str]:
    """
    These bikes' ids in the order *the database* puts ``(rider_name, id)``.

    The oracle for the ordering tests, and deliberately not Python's
    ``sorted()`` (t-bike-order-collation). Python compares code points, which
    agrees with a byte-order (``C``) collation by construction and with glibc or
    ICU collations not at all — Neon orders ``alex, Alex, ALEX, Ana, Ána, Zoe``
    where ``sorted()`` gives ``ALEX, Alex, Ana, Zoe, alex, Ána``. Asking Postgres
    makes the expected sequence whatever the columns' collation says it is, so
    the test states the repository's promise (``ORDER BY rider_name, id``)
    without also asserting which collation the server happens to run.
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(tables.bikes.c.id)
            .where(tables.bikes.c.id.in_(bike_ids))
            .order_by(tables.bikes.c.rider_name, tables.bikes.c.id)
        )
        return [row.id for row in rows]


async def test_bikes_are_ordered_by_rider_name_then_id(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
) -> None:
    """
    The declared order, including the id tiebreak between two bikes of one rider.

    The expected sequence is computed from the fixture's own rows rather than
    written out, so it stays correct if the fixture changes, and is ordered by
    the database rather than by Python — see ``database_order``.
    """
    trip = seeded_trips[0]
    expected = await database_order(
        migrated_engine, [b.id for b in seeded_bikes if b.trip_id == trip.id]
    )
    assert len(expected) == 3, "the fixture's first-trip bikes are missing — nothing to order"

    body = (await client.get(TRIP_PATH.format(slug=trip.rider_slug))).json()

    assert [bike["id"] for bike in body["bikes"]] == expected


# Rider names that differ only in case or accent — the names on which a
# byte-order collation and a linguistic one disagree. Inserted in an order that
# matches neither, so insertion order cannot pass for the promised order.
COLLATION_SENSITIVE_RIDERS = ["Zoe", "ALEX", "Ána", "alex", "Ana", "Alex"]


@pytest.fixture
async def collation_sensitive_bikes(
    migrated_engine: AsyncEngine, trip_without_bikes: SeededTrip
) -> AsyncIterator[list[str]]:
    """Bike ids on the otherwise bikeless trip, one per ``COLLATION_SENSITIVE_RIDERS`` name."""
    prefix = f"test-bike-coll-{secrets.token_urlsafe(8)}"
    ids = [f"{prefix}-{index:02d}" for index in range(len(COLLATION_SENSITIVE_RIDERS))]

    async with migrated_engine.begin() as conn:
        for bike_id, rider in zip(ids, COLLATION_SENSITIVE_RIDERS, strict=True):
            await conn.execute(
                tables.bikes.insert().values(
                    id=bike_id,
                    trip_id=trip_without_bikes.id,
                    rider_name=rider,
                    make="Honda",
                    model="XR650L",
                    year=2020,
                    specs="",
                )
            )
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.bikes.delete().where(tables.bikes.c.id.in_(ids)))


async def test_bike_order_holds_for_case_and_accent_variants(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    trip_without_bikes: SeededTrip,
    collation_sensitive_bikes: list[str],
) -> None:
    """
    The same promise on names where collations disagree, so it holds on Neon too.

    The local compose Postgres collates byte-wise and Neon runs glibc/ICU; this
    case passes on both because the oracle is the database's own ordering.
    """
    expected = await database_order(migrated_engine, collation_sensitive_bikes)
    assert sorted(expected) == sorted(collation_sensitive_bikes)

    body = (await client.get(TRIP_PATH.format(slug=trip_without_bikes.rider_slug))).json()

    assert [bike["id"] for bike in body["bikes"]] == expected


# --------------------------------------------------------------------------
# 7. No slug ever comes back
# --------------------------------------------------------------------------


async def test_no_slug_appears_in_the_response_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    Neither slug of any trip appears anywhere in a successful body.

    A slug is the credential, and unlike a password it cannot be rotated without
    re-sending the link to everyone who has it. Reflecting one into a body puts
    it wherever the body ends up — a log line, a screenshot in a bug report, a
    browser cache, an error-reporting service.

    Asserted as a substring search over the raw text, not over parsed fields: the
    leak this guards against is a field nobody expected, and a whitelist of
    fields to check would be a list of the leaks already thought of.

    The 200 assertion in front of it is load-bearing, not a courtesy. Any
    response that failed would trivially contain no slug, so without it this
    test would go green on a broken endpoint — and would be green for exactly
    the mutation (spreading the ``TripRecord`` into ``TripOut``) that this test
    exists to catch.
    """
    first, second = seeded_trips

    for trip in (first, second):
        for slug in (trip.rider_slug, trip.viewer_slug):
            response = await client.get(TRIP_PATH.format(slug=slug))

            assert response.status_code == HTTPStatus.OK, response.text
            for candidate in (
                first.rider_slug,
                first.viewer_slug,
                second.rider_slug,
                second.viewer_slug,
            ):
                assert candidate not in response.text, f"{candidate!r} leaked into the body"


async def test_no_slug_appears_in_the_404_body(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    Nor in the failure body — an error message is the part most likely to be logged.

    The unknown slug itself is included in the check: echoing back whatever the
    caller sent is the other half of the same mistake, and a 404 body that
    quoted the slug would carry a real one every time a rider's link expired.
    """
    trip = seeded_trips[0]

    for slug in (trip.viewer_slug, UNKNOWN_SLUG):
        response = await client.get(TRIP_PATH.format(slug=f"{slug}-nope"))

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert slug not in response.text


# --------------------------------------------------------------------------
# 8. Wiring — the route goes through the shared read guard
# --------------------------------------------------------------------------


def _declared_dependencies(func: Any) -> list[Any]:
    """
    The callables a function declares via ``Depends`` in its own signature.

    ``eval_str=True`` so this keeps working if the module ever adopts
    ``from __future__ import annotations`` — without it every annotation would
    be a plain string and the ``Annotated`` metadata invisible here, which would
    make the tests below silently vacuous rather than failing.
    """
    found: list[Any] = []
    for parameter in inspect.signature(func, eval_str=True).parameters.values():
        for metadata in get_args(parameter.annotation)[1:]:
            if isinstance(metadata, params.Depends):
                found.append(metadata.dependency)
    return found


def test_route_declares_require_trip_access() -> None:
    """
    The handler takes its trip from the shared read guard, not a lookup of its own.

    ``require_trip_access`` is where the 403/404 distinction and the ``access``
    derivation live (spec Section 12's top-priority area). A handler that
    resolved the slug itself could pass every behavioural test above while
    quietly owning a second copy of the rule that the other seven endpoints
    would then not share.
    """
    from app.api.routes.trips import get_trip

    assert require_trip_access in _declared_dependencies(get_trip)


def test_route_does_not_declare_the_write_guard() -> None:
    """
    ...and specifically not the write gate, ``require_trip_writer``.

    Stated separately because the damage is one-sided and silent: the rider's
    own session would keep working perfectly, so every test run by whoever made the
    change would pass, while every anonymous reader got a 401 on the app's first
    request.
    """
    from app.api.routes.trips import get_trip

    assert require_trip_writer not in _declared_dependencies(get_trip)


def test_route_takes_its_session_from_get_session() -> None:
    """
    The handler's session comes from ``get_session`` — the callable the fixture overrides.

    Without this, the HTTP tests would keep passing against a handler that had
    started opening its own connection: the override would simply never fire and
    nothing would say so.
    """
    from app.api.routes.trips import get_trip

    assert get_session in _declared_dependencies(get_trip)


def test_route_is_registered_at_the_contract_path() -> None:
    """
    ``get_trip`` is mounted at ``/api/trips/{slug}`` — the path the contract names.

    Resolved through ``url_path_for`` rather than by scanning ``app.routes``:
    this FastAPI version keeps an included router as a single lazy entry in that
    list instead of flattening its routes into it, so a scan finds nothing and
    would fail against a perfectly correct app. ``url_path_for`` asks the router
    the same question the router answers for a real request, and is public API.
    """
    import app.main

    assert app.main.app.url_path_for("get_trip", slug="a-slug") == "/api/trips/a-slug"


# --------------------------------------------------------------------------
# 9. The OpenAPI spec — what Kubb generates the frontend client from
# --------------------------------------------------------------------------
# Asserted in a test rather than checked by eye. The spec is not documentation
# here: it is the input to the generated client (`frontend/src/api/`), so a
# missing response model or an undescribed field becomes an untyped or
# unexplained call site in a different part of the codebase entirely.


def _openapi() -> dict[str, Any]:
    import app.main

    return app.main.app.openapi()


def test_openapi_declares_the_trip_path() -> None:
    """The endpoint is in the spec at the contract's path, as a GET."""
    paths = _openapi()["paths"]

    assert OPENAPI_PATH in paths
    assert "get" in paths[OPENAPI_PATH]


def test_openapi_200_response_is_trip_out() -> None:
    """A 200 is typed as ``TripOut`` — without it the generated hook returns `any`."""
    operation = _openapi()["paths"][OPENAPI_PATH]["get"]

    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "TripOut"


def test_openapi_404_response_is_the_error_envelope() -> None:
    """
    A 404 is typed as ``ErrorEnvelope``.

    The contract lists 404 for this endpoint, and the envelope is what makes one
    error-handling path possible on the frontend. An undeclared 404 is invisible
    to Kubb, so the generated client would have no type for the single failure
    this endpoint can produce.
    """
    operation = _openapi()["paths"][OPENAPI_PATH]["get"]

    assert "404" in operation["responses"], "the 404 response is not declared"
    schema = operation["responses"]["404"]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "ErrorEnvelope"


def test_openapi_components_include_the_three_models() -> None:
    """``TripOut``, ``BikeOut`` and ``ErrorEnvelope`` are all emitted as schemas."""
    schemas = _openapi()["components"]["schemas"]

    assert {"TripOut", "BikeOut", "ErrorEnvelope"} <= set(schemas)


def test_openapi_route_description_follows_the_mandated_format() -> None:
    """
    The route's description is Context -> How it works -> Related APIs, and names its task.

    Spec Section 5 makes that format mandatory, and this is the text every
    downstream reader gets: the generated client's doc comment, the Swagger page
    and every agent's context. Asserted on the spec rather than the source,
    because the source string only matters insofar as it reaches here.
    """
    description = _openapi()["paths"][OPENAPI_PATH]["get"]["description"]

    assert "Context" in description
    assert "How it works" in description
    assert "Related APIs" in description
    assert "t-trip-metadata-endpoint" in description
    assert len(description) > 400, "a heading-only description is not a description"


@pytest.mark.parametrize("model", [TripOut, BikeOut])
def test_every_response_field_has_a_real_description(model: type) -> None:
    """
    Every field of both response models carries a description, not just a type.

    ``description=`` is what makes the OpenAPI spec trustworthy — it is the only
    thing that explains ``access`` to a frontend developer reading a generated
    hook, or tells them ``specs`` is ``""`` and never null. A length floor
    because ``description="id"`` would satisfy a presence check while saying
    nothing.
    """
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"
        assert len(field.description) > 25, f"{model.__name__}.{name}: description is a stub"


@pytest.mark.parametrize("model", [TripOut, BikeOut])
def test_descriptions_survive_into_the_openapi_schema(model: type) -> None:
    """
    ...and they actually reach the spec, which is the only place they are consumed.

    A model-level check alone would pass even if the schema were emitted from
    something else — the two are asserted separately for the same reason the
    status and the code are.
    """
    properties = _openapi()["components"]["schemas"][model.__name__]["properties"]

    for name in model.model_fields:
        assert properties[name].get("description"), f"{model.__name__}.{name} lost its description"


# --------------------------------------------------------------------------
# 10. The repository underneath, called directly
# --------------------------------------------------------------------------


async def test_repository_returns_only_the_given_trips_bikes(
    db_session: AsyncSession, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """The filter, asserted at the layer that owns it rather than through HTTP."""
    first, second = seeded_trips

    result = await list_by_trip(db_session, first.id)

    assert {bike.id for bike in result} == {b.id for b in seeded_bikes if b.trip_id == first.id}
    assert all(bike.id != b.id for bike in result for b in seeded_bikes if b.trip_id == second.id)


async def test_repository_returns_bike_out_models(
    db_session: AsyncSession, seeded_trips: list[SeededTrip], seeded_bikes: list[SeededBike]
) -> None:
    """
    What leaves ``data/`` is the contract model, never a raw row.

    The layering rule from ``tables.py``: repositories map rows to the models in
    ``app/models/`` themselves, so no caller ever indexes a row by column name
    and the API contract stays the only definition of a response shape.
    """
    first = seeded_trips[0]
    expected = next(b for b in seeded_bikes if b.trip_id == first.id)

    result = await list_by_trip(db_session, first.id)

    assert all(isinstance(bike, BikeOut) for bike in result)
    found = next(bike for bike in result if bike.id == expected.id)
    assert found.riderName == expected.rider_name


async def test_repository_returns_an_empty_list_for_a_trip_with_no_bikes(
    db_session: AsyncSession, trip_without_bikes: SeededTrip, seeded_bikes: list[SeededBike]
) -> None:
    """No bikes is ``[]``, not ``None`` and not an exception — with other trips' bikes present."""
    result = await list_by_trip(db_session, trip_without_bikes.id)

    assert result == []


async def test_repository_returns_an_empty_list_for_an_unknown_trip_id(
    db_session: AsyncSession, seeded_bikes: list[SeededBike]
) -> None:
    """
    An id no trip has is ``[]``, not an error.

    Whether "nothing found" is a 404 is a transport decision made in
    ``core/security.py``, and by the time this function is called the trip has
    already been resolved — so there is nothing here for it to raise about.
    """
    assert await list_by_trip(db_session, "no-such-trip-id-0000") == []
