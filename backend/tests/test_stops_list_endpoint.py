"""
``GET /api/trips/{slug}/stops`` — the stop list, asserted against the contract.

The assertions here are written from ``docs/api-contract.md`` and the task's
acceptance criteria, not from the handler: two of the three promises this
endpoint makes are spec Section 12 priority categories, and a test derived from
the implementation would encode the implementation's misunderstanding as the
expected answer.

What the contract promises for this row:

1. **Either slug reads it** (endpoint table, "Slug accepted: either"). A viewer
   slug is an ordinary 200 here — this is a read, and 403 is only ever reachable
   on a write.
2. **An unknown slug is 404 / ``NOT_FOUND``, never 403** ("Access control: 403
   and 404 are different answers"). A 403 for a slug nothing resolves to turns
   the slug space into an oracle: it would tell whoever is guessing that they
   can distinguish "wrong slug" from "right slug, wrong permission", which is
   the one signal the unguessable-slug model depends on not leaking.
3. **A trip's stops are that trip's stops.** A missing ``WHERE trip_id`` serves
   one trip's journal under another trip's link, which is the top-priority
   failure class. Both seeded trips carry stops here on purpose: with stops on
   only one of them, a query that ignored its filter and one that applied it are
   indistinguishable (decision-log entry 7b).

Plus the shape promises that a generated client is typed off: ``StopOut``'s key
set in camelCase, ``locationSource`` as ``"gps"``/``"manual"``, and ``notes``
round-tripping ``null`` as ``null`` — that column is nullable, unlike
``bikes.specs``, so null is the real "nothing written" value and not a bug.

**Order is deliberately not asserted.** The repository orders by
``arrived_at, id`` for response *stability*; the contract promises no order for
this endpoint, so every test below looks stops up by ``id``. The one test that
touches order at all asserts stability (two identical requests, identical body)
and says why that is safe.

Setup follows ``test_trip_metadata_endpoint.py``: the real app, a real database,
and ``get_session`` overridden onto this test's engine because the app-level
engine pools asyncpg connections bound to whichever event loop first used them.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from typing import Any

import pytest
from conftest import SeededTrip, make_async_client
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope
from app.models.stop import StopOut

# The route as a client sees it, and the same path as FastAPI renders it into
# the OpenAPI document.
STOPS_PATH = "/api/trips/{slug}/stops"
OPENAPI_PATH = "/api/trips/{slug}/stops"

# A slug no trip has. Fixed rather than random so a failure says what was asked.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# Exactly the keys `StopOut` promises, written out rather than derived from the
# model: deriving them would make the assertion "the response matches the model"
# when what is under test is the model still matching the written contract.
CONTRACT_KEYS = {"id", "name", "lat", "lng", "locationSource", "arrivedAt", "notes"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededStop:
    """
    A stop row this fixture put in the database, in the database's own spelling.

    Snake_case on purpose — ``location_source``, ``arrived_at``. This is the
    independent statement of what is in the *table*, and the mapping onto the
    contract's camelCase is precisely the thing under test; a fixture spelling
    it the API's way would make that mapping unobservable.
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
        async with make_async_client(application) as http_client:
            yield http_client
    finally:
        application.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def seeded_stops(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[list[SeededStop]]:
    """
    Stops on **both** seeded trips, removed again on teardown.

    Both trips, and that is the point of the fixture rather than a detail of it.
    Cross-trip isolation is the assertion that fails when the repository stops
    filtering on ``trip_id`` — and it can only fail if another trip's stops are
    actually in the table. The second trip's stop deliberately carries the same
    ``name`` as one on the first, so a leak produces a body that still reads
    plausibly to a human and only the id set catches it.

    Values are all distinct across fields (no stop's ``name`` equals another's
    ``notes``, no two share coordinates) so a mapping that passed the wrong
    column to the wrong response field cannot produce a body that still matches.

    Coverage carried by the rows themselves:

    - one ``gps`` and one ``manual`` stop on the first trip, so
      ``locationSource`` round-trips both values rather than one;
    - one stop with ``notes = NULL``, which must come back as ``null``;
    - two stops sharing an ``arrived_at``, so the repository's stability
      ordering has a tie to break (asserted only as stability — see the ordering
      section).

    Ids are one shared random prefix with a numeric suffix, and rows are
    inserted in an order that is neither the repository's ordering nor its
    reverse, so a handler returning rows in insertion order would not
    accidentally match. Cleanup is by id, like ``seeded_bikes``: the ``trips``
    delete would cascade (``stops.trip_id`` is ``ON DELETE CASCADE``), but
    relying on that would strand any stop attached to some other trip.
    """
    first, second = seeded_trips
    prefix = f"test-stop-{secrets.token_urlsafe(8)}"
    base = datetime(2026, 6, 2, 8, 30, tzinfo=UTC)

    seeded = [
        SeededStop(
            id=f"{prefix}-02",
            trip_id=first.id,
            name="Daly Waters Pub",
            lat=-16.2503,
            lng=133.3703,
            location_source="gps",
            arrived_at=base + timedelta(hours=6),
            notes="Schnitzel, and a bra on the ceiling.",
        ),
        SeededStop(
            id=f"{prefix}-01",
            trip_id=first.id,
            name="Katherine Hot Springs",
            lat=-14.4652,
            lng=132.2635,
            location_source="manual",
            arrived_at=base,
            # NULL notes: the column is nullable (unlike bikes.specs) and this
            # must come back as `null`, not as "" and not as a missing key.
            notes=None,
        ),
        SeededStop(
            id=f"{prefix}-03",
            trip_id=first.id,
            name="Threeways Roadhouse",
            lat=-19.3097,
            lng=134.2098,
            location_source="gps",
            # Same instant as -02: a tie for the stability ordering to break.
            arrived_at=base + timedelta(hours=6),
            notes="Fuel and a pie.",
        ),
        # The other trip entirely — and it shares a name with a stop above.
        SeededStop(
            id=f"{prefix}-04",
            trip_id=second.id,
            name="Daly Waters Pub",
            lat=-31.9523,
            lng=115.8613,
            location_source="manual",
            arrived_at=base + timedelta(days=30),
            notes="Belongs to the other trip and must never appear in the first's.",
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


@pytest.fixture
async def trip_without_stops(migrated_engine: AsyncEngine) -> AsyncIterator[SeededTrip]:
    """
    A third trip that has no stops, created *alongside* trips that do.

    The empty case could be tested by simply not asking for ``seeded_stops`` —
    but then it would pass against a ``stops`` table with nothing in it, which is
    decision-log entry 7b's failure mode exactly: green because the condition was
    never created. Used together with ``seeded_stops``, this proves ``[]`` is an
    answer about *this trip* rather than an accident of an empty table.
    """
    trip = SeededTrip(
        id=f"test-trip-{secrets.token_urlsafe(8)}",
        name="Trip with no stops",
        start_date=date(2026, 8, 9),
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


def expected_stop_body(stop: SeededStop) -> dict[str, Any]:
    """
    The JSON one seeded stop must serialise to — written from the *database* row.

    ``arrivedAt`` is left as a ``datetime`` here and the response's string is
    parsed back before comparison: Postgres returns the instant in its own
    offset spelling, and asserting on the text would be asserting on the
    driver's formatting rather than on the instant the rider recorded.
    """
    return {
        "id": stop.id,
        "name": stop.name,
        "lat": stop.lat,
        "lng": stop.lng,
        "locationSource": stop.location_source,
        "arrivedAt": stop.arrived_at,
        "notes": stop.notes,
    }


def stops_by_id(body: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    The response's stops keyed by id, with ``arrivedAt`` parsed — never indexed positionally.

    The contract promises no order for this endpoint (the repository's
    ``ORDER BY`` is response stability only), so matching by position would
    quietly turn an implementation detail into a promise.
    """
    return {
        stop["id"]: {**stop, "arrivedAt": datetime.fromisoformat(stop["arrivedAt"])}
        for stop in body
    }


# --------------------------------------------------------------------------
# 1. Either slug reads — and the two see the same journal
# --------------------------------------------------------------------------


async def test_rider_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """The baseline: the rider's own link lists their stops."""
    trip = seeded_trips[0]

    response = await client.get(STOPS_PATH.format(slug=trip.rider_slug))

    assert response.status_code == HTTPStatus.OK


async def test_viewer_slug_returns_200(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    A viewer link lists the stops too — the contract says "either" for this row.

    This is the assertion that fails if the route is ever switched to the write
    guard: every viewer in existence would get a 403 on the journal itself, and
    the only report would be "the link you sent me doesn't work".
    """
    trip = seeded_trips[0]

    response = await client.get(STOPS_PATH.format(slug=trip.viewer_slug))

    assert response.status_code == HTTPStatus.OK


async def test_viewer_slug_is_not_forbidden(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """Stated as its own negative: a 403 on a read endpoint is the regression to catch."""
    trip = seeded_trips[0]

    response = await client.get(STOPS_PATH.format(slug=trip.viewer_slug))

    assert response.status_code != HTTPStatus.FORBIDDEN


async def test_both_slugs_return_an_identical_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    Byte-identical: unlike ``TripOut``, nothing in this response may differ by slug.

    ``GET /trips/{slug}`` differs on ``access``; ``StopOut`` has no such field,
    so a viewer seeing a shorter list, different notes or different coordinates
    would mean the read-only link showed a different journal from the one the
    rider shared.
    """
    trip = seeded_trips[0]

    rider = await client.get(STOPS_PATH.format(slug=trip.rider_slug))
    viewer = await client.get(STOPS_PATH.format(slug=trip.viewer_slug))

    assert rider.status_code == HTTPStatus.OK
    assert viewer.status_code == HTTPStatus.OK
    assert rider.json() == viewer.json()


# --------------------------------------------------------------------------
# 2. An unknown slug — 404, in the envelope, and never 403
# --------------------------------------------------------------------------


async def test_unknown_slug_is_not_found(client: AsyncClient) -> None:
    """A slug no trip has resolves to nothing, and nothing is a 404."""
    response = await client.get(STOPS_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code == HTTPStatus.NOT_FOUND


async def test_unknown_slug_is_never_forbidden(client: AsyncClient) -> None:
    """
    ...and specifically **not** a 403, which is the oracle the slug model rests on.

    A 403 here would confirm to anyone guessing URLs that they can tell "wrong
    slug" from "right slug, wrong permission". Asserted separately from the 404
    so the failure names the leak rather than a status mismatch.
    """
    response = await client.get(STOPS_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code != HTTPStatus.FORBIDDEN


async def test_unknown_slug_reports_the_not_found_envelope(client: AsyncClient) -> None:
    """
    The body parses as ``ErrorEnvelope`` with ``code == "NOT_FOUND"``.

    The code is asserted as well as the status because the offline queue branches
    on ``code``, not on the status line, to decide whether a request can ever
    succeed.
    """
    response = await client.get(STOPS_PATH.format(slug=UNKNOWN_SLUG))

    assert response.headers["content-type"] == "application/json"
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code is ErrorCode.NOT_FOUND
    assert envelope.error.message


# --------------------------------------------------------------------------
# 3. Cross-trip isolation — this trip's stops, and no others
# --------------------------------------------------------------------------


async def test_stops_are_exactly_this_trips_stops(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    The id set matches the trip's own stops — nothing missing, nothing borrowed.

    The pre-assertion is load-bearing, not ceremony: this test is the one that
    fails when the repository stops filtering on ``trip_id``, and it can only
    fail while the other trip actually has stops. If the fixture ever stopped
    seeding both trips, this would pass while checking nothing (entry 7b).
    """
    first, second = seeded_trips
    expected = {s.id for s in seeded_stops if s.trip_id == first.id}
    foreign = {s.id for s in seeded_stops if s.trip_id == second.id}
    assert expected and foreign, "the fixture must seed stops on both trips for this to bite"

    body = (await client.get(STOPS_PATH.format(slug=first.rider_slug))).json()

    assert set(stops_by_id(body)) == expected


async def test_the_other_trips_slug_returns_the_other_trips_stops(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    The same claim from the other side: trip B's link returns B's stops, not A's.

    Asserted both ways round because a handler that returned every stop in the
    table would satisfy neither, but one that returned a hardcoded or
    first-matching trip's stops would satisfy exactly one.
    """
    _, second = seeded_trips

    body = (await client.get(STOPS_PATH.format(slug=second.viewer_slug))).json()

    assert set(stops_by_id(body)) == {s.id for s in seeded_stops if s.trip_id == second.id}


async def test_no_other_trips_stop_appears_in_the_response(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    The same fact stated as the leak it would be, over the raw text.

    Named separately from the id-set test because this is the failure a reviewer
    should be able to find by name when it happens: one trip's journal served
    under another trip's link. The foreign stop shares a ``name`` with one of
    this trip's, so its id and notes are what actually distinguish it.
    """
    first, second = seeded_trips
    foreign = [s for s in seeded_stops if s.trip_id == second.id]
    assert foreign, "the fixture must seed a stop on the other trip"

    response = await client.get(STOPS_PATH.format(slug=first.rider_slug))

    assert response.status_code == HTTPStatus.OK
    for stop in foreign:
        assert stop.id not in response.text
        assert stop.notes and stop.notes not in response.text


async def test_a_trip_with_no_stops_returns_an_empty_list(
    client: AsyncClient,
    trip_without_stops: SeededTrip,
    seeded_stops: list[SeededStop],
) -> None:
    """
    200 with ``[]`` — never a 404, never ``null``.

    An empty collection is not a missing one: every trip has no stops until the
    rider adds the first, and a 404 there would tell a viewer their link is
    broken on the first day of the trip. Run with ``seeded_stops`` active, so the
    table is *not* empty and ``[]`` is a real answer about this trip.
    """
    response = await client.get(STOPS_PATH.format(slug=trip_without_stops.rider_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


async def test_a_trip_with_no_stops_is_the_same_on_the_viewer_slug(
    client: AsyncClient,
    trip_without_stops: SeededTrip,
    seeded_stops: list[SeededStop],
) -> None:
    """The empty case is a read like any other — the viewer link is not a 403 or a 404."""
    response = await client.get(STOPS_PATH.format(slug=trip_without_stops.viewer_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


# --------------------------------------------------------------------------
# 4. The body shape — `StopOut`, in camelCase, every field from its own row
# --------------------------------------------------------------------------


async def test_stop_objects_have_exactly_the_contract_keys(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    ``{id, name, lat, lng, locationSource, arrivedAt, notes}`` — no more, no fewer.

    A missing key breaks the generated client's type. An extra one is worse than
    untidy: ``trip_id`` is the neighbouring column, and a response that grew
    fields by accident is how internal identifiers reach a log or a screenshot.
    """
    trip = seeded_trips[0]

    body = (await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json()

    assert body, "this trip must have stops for the assertion to mean anything"
    for stop in body:
        assert set(stop.keys()) == CONTRACT_KEYS


async def test_each_stop_carries_every_field_from_its_own_row(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    Whole-object equality against the database row, per stop.

    Not field-by-field spot checks: the mapping is seven keyword arguments
    written by hand, and the mistake that shape invites is passing the wrong
    column to one of them (``name=row.notes``, ``lat=row.lng``). The fixture
    gives every field distinct values precisely so a crossed pair cannot produce
    a body that still matches — including ``lat``/``lng``, which are both plain
    floats and would swap silently.
    """
    trip = seeded_trips[0]
    expected = {s.id: expected_stop_body(s) for s in seeded_stops if s.trip_id == trip.id}

    body = (await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json()

    assert stops_by_id(body) == expected


async def test_location_source_round_trips_both_values(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    ``"gps"`` and ``"manual"`` both survive the round trip, spelled as the contract spells them.

    Both values, not one: a mapping that returned a constant, or that collapsed
    the column onto a default, matches on whichever value it happens to emit.
    This field is how a viewer tells an exact fix from an approximate map tap,
    and it is the only evidence that the GPS-denied fallback path actually ran.
    """
    trip = seeded_trips[0]
    expected = {s.id: s.location_source for s in seeded_stops if s.trip_id == trip.id}
    assert set(expected.values()) == {"gps", "manual"}, "the fixture must seed both values"

    body = (await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json()

    assert {id_: stop["locationSource"] for id_, stop in stops_by_id(body).items()} == expected


async def test_notes_round_trips_null_as_null(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    A stop with nothing written comes back ``null`` — not ``""``, not a missing key.

    ``stops.notes`` is nullable, unlike ``bikes.specs`` (``NOT NULL DEFAULT ''``),
    so the two columns have opposite empty values and a handler that reused the
    bike treatment here would turn "nothing written" into an empty string. The
    non-null stop is asserted in the same test so a handler that nulled
    *everything* cannot pass.
    """
    trip = seeded_trips[0]
    blank = next(s for s in seeded_stops if s.trip_id == trip.id and s.notes is None)
    written = next(s for s in seeded_stops if s.trip_id == trip.id and s.notes is not None)

    body = stops_by_id((await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json())

    assert "notes" in body[blank.id]
    assert body[blank.id]["notes"] is None
    assert body[written.id]["notes"] == written.notes


async def test_body_validates_against_the_contract_model(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    The body parses as ``list[StopOut]`` — the shape the generated client expects.

    Complements the key-set assertion: this one checks the *types* a key
    comparison cannot see — ``arrivedAt`` as a real instant, ``locationSource``
    as one of the two literals.
    """
    trip = seeded_trips[0]

    body = (await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json()

    parsed = [StopOut.model_validate(stop) for stop in body]
    assert {stop.id for stop in parsed} == {s.id for s in seeded_stops if s.trip_id == trip.id}


async def test_arrived_at_is_timezone_aware(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    ``arrivedAt`` carries an offset — it is an instant, not a local wall clock.

    The column is ``TIMESTAMPTZ`` and the value was captured on a device that may
    have been offline. Serialised without an offset, every viewer outside the
    rider's timezone would read the wrong arrival time, and nothing else in the
    response would look wrong.
    """
    trip = seeded_trips[0]

    body = (await client.get(STOPS_PATH.format(slug=trip.rider_slug))).json()

    assert body
    for stop in body:
        assert datetime.fromisoformat(stop["arrivedAt"]).tzinfo is not None, stop["arrivedAt"]


# --------------------------------------------------------------------------
# 5. No slug ever comes back
# --------------------------------------------------------------------------


async def test_no_slug_appears_in_the_response_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    Neither slug of either trip appears anywhere in a successful body.

    The slug is the credential, and unlike a password it cannot be rotated
    without re-sending the link to everyone holding it. Asserted as a substring
    search over the raw text rather than over parsed fields: the leak this guards
    against is a field nobody expected, and a whitelist would be a list of the
    leaks already thought of.

    The 200 assertion is load-bearing — any failing response would trivially
    contain no slug, so without it this test would go green on a broken endpoint.
    """
    first, second = seeded_trips

    for trip in (first, second):
        for slug in (trip.rider_slug, trip.viewer_slug):
            response = await client.get(STOPS_PATH.format(slug=slug))

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

    The unknown slug itself is checked too: echoing back whatever the caller sent
    is the other half of the same mistake, and it would carry a real slug every
    time a rider mistyped their own link.
    """
    trip = seeded_trips[0]

    for slug in (trip.viewer_slug, UNKNOWN_SLUG):
        response = await client.get(STOPS_PATH.format(slug=f"{slug}-nope"))

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert slug not in response.text


# --------------------------------------------------------------------------
# 6. HEAD — the same guard runs, with no body
# --------------------------------------------------------------------------
# `test_head_method.py` already asserts, generically over every documented GET
# path, that HEAD answers and agrees with GET — but it fills path parameters with
# an unknown slug, so for this endpoint it compares two 404s. What it cannot see
# is whether the access dependency runs on the HEAD route at all: a HEAD sibling
# wired to a stub, or one registered without the dependency, agrees with GET on
# the unknown slug and diverges only on a real one.


async def test_head_on_either_slug_is_200_with_no_body(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """HEAD is GET without a body (RFC 9110 §9.3.2), on both the rider and viewer links."""
    trip = seeded_trips[0]

    for slug in (trip.rider_slug, trip.viewer_slug):
        response = await client.head(STOPS_PATH.format(slug=slug))

        assert response.status_code == HTTPStatus.OK
        assert not response.content, "HEAD must not carry a body"


async def test_head_on_an_unknown_slug_is_404(client: AsyncClient) -> None:
    """
    ...and an unknown slug is the same 404 GET gives — the dependency really runs.

    This is the assertion that separates "HEAD is served by the real handler"
    from "HEAD is served by *something*": a route that skipped
    ``require_trip_access`` would answer 200 here, which is the shape in which a
    verb quietly stops being access-controlled.
    """
    response = await client.head(STOPS_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# 7. Order — stability only, deliberately not a contract promise
# --------------------------------------------------------------------------
# The repository orders by `arrived_at, id` so two identical requests produce
# identical JSON. That is a stability property and **not** an API guarantee: the
# contract promises no order for this endpoint, and the map endpoint establishes
# its own chronology rather than inheriting one from here. Asserting the actual
# sequence would convert an implementation detail into a promise the API does not
# make — so the only test here is that the body does not churn, which is safe
# because it holds for *any* total ordering the repository might choose. Every
# other test in this file looks stops up by id.


async def test_the_response_is_stable_across_identical_requests(
    client: AsyncClient, seeded_trips: list[SeededTrip], seeded_stops: list[SeededStop]
) -> None:
    """
    The same request twice gives byte-identical bodies.

    Without an ``ORDER BY``, Postgres may return rows in any order it likes, and
    a body that reshuffles between calls turns every cached or diffed copy of it
    into noise. The fixture seeds two stops with the same ``arrived_at`` so the
    ordering has a tie to break; without the ``id`` tiebreak this is exactly
    where a reshuffle would appear.
    """
    trip = seeded_trips[0]
    path = STOPS_PATH.format(slug=trip.rider_slug)

    first = await client.get(path)
    second = await client.get(path)

    assert first.text == second.text


# --------------------------------------------------------------------------
# 8. The OpenAPI document — what Kubb generates the frontend client from
# --------------------------------------------------------------------------


def _openapi() -> dict[str, Any]:
    import app.main

    return app.main.app.openapi()


def test_openapi_200_response_is_an_array_of_stop_out() -> None:
    """
    The endpoint is documented as a GET returning ``StopOut[]``.

    Without the item type the generated hook returns ``any[]``, and the frontend
    loses every field description this endpoint is the sole source of
    (``notes``, ``locationSource``).
    """
    paths = _openapi()["paths"]

    assert OPENAPI_PATH in paths, list(paths)
    schema = paths[OPENAPI_PATH]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["type"] == "array"
    assert schema["items"]["$ref"].rsplit("/", 1)[-1] == "StopOut"


def test_openapi_404_response_is_the_error_envelope() -> None:
    """
    A 404 is declared and typed as ``ErrorEnvelope`` — the contract lists it for this row.

    An undeclared 404 is invisible to Kubb, so the generated client would have no
    type for the only failure this endpoint can produce.
    """
    operation = _openapi()["paths"][OPENAPI_PATH]["get"]

    assert "404" in operation["responses"], "the 404 response is not declared"
    schema = operation["responses"]["404"]["content"]["application/json"]["schema"]
    assert schema["$ref"].rsplit("/", 1)[-1] == "ErrorEnvelope"


def test_openapi_route_description_follows_the_mandated_format() -> None:
    """
    Context -> How it works -> Related APIs, naming its task.

    Mandatory per CLAUDE.md, and this text is what every downstream reader gets:
    the generated client's doc comment, the Swagger page, and every agent's
    context.
    """
    description = _openapi()["paths"][OPENAPI_PATH]["get"]["description"]

    assert "Context" in description
    assert "How it works" in description
    assert "Related APIs" in description
    assert "t-stops-list-endpoint" in description
    assert len(description) > 400, "a heading-only description is not a description"


def test_every_stop_out_field_has_a_description_that_reaches_the_spec() -> None:
    """
    Every ``StopOut`` field carries a real description, and it survives into the document.

    Both halves: the model check alone would pass even if the schema were emitted
    from something else, and the spec is the only place these descriptions are
    consumed. A length floor because ``description="id"`` satisfies a presence
    check while saying nothing.
    """
    properties = _openapi()["components"]["schemas"]["StopOut"]["properties"]

    for name, field in StopOut.model_fields.items():
        assert field.description, f"StopOut.{name} has no description"
        assert len(field.description) > 25, f"StopOut.{name}: description is a stub"
        assert properties[name].get("description"), f"StopOut.{name} lost its description"
