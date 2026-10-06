"""
No slug value ever comes back from a real ``/api/trips`` route.

A slug **is** the credential in this app — there are no accounts and no second
factor, so the rider slug is the write password and the viewer slug is the read
password. ``app/core/security.py`` is explicit that nothing in it puts a slug in
a message or a body, and ``test_slug_access.py`` §6 holds that guarantee against
the two *probe* routes it builds for itself. Probe routes are not where the
mistake happens. The mistake happens in a real handler, and it is a one-liner:
``require_rider_access`` hands the handler a ``TripContext`` whose ``trip``
record carries **both** slugs (it has to — that is how ``access_for_slug``
derives the permission), so ``return {**asdict(context.trip)}``, or a
``TripOut`` that gains a field by copy-paste, puts the *rider* slug — the write
credential — into a *viewer's* browser, their browser cache, their bug report
screenshot and any log that captured the response. Unlike a password it cannot
be rotated without re-issuing the link to everyone who has it.

This module generalises §6 from the probes to every route the app actually
registers. Four things about how it is written are load-bearing:

**The route list comes from the audit module, not from here.**
``test_route_dependency_audit._api_routes`` walks FastAPI's lazy
``_IncludedRouter`` tree (see that module's docstring — a naive scan of
``app.routes`` sees *none* of the contract endpoints and passes vacuously). The
enumeration lives in one place on purpose; a second copy is a second thing to
keep correct, and the copy that drifts is the one that stops auditing. A route
added tomorrow is driven tomorrow — or, if nobody added it to ``EXPECTED_STATUS``
below, ``test_every_registered_trip_route_has_a_plan`` fails rather than the
route going quietly unaudited.

**The assertion is on the slug *values*, never the key names.** Grepping a body
for ``"rider_slug"`` would sail straight past a handler that serialises the same
string as ``riderSlug``, or as ``credential``, or inside a rendered error
message. The credential is the value; the value is what is asserted, against the
raw response text *and* the response headers.

**Every case asserts its status code first.** An audit like this fails open: a
route that 422s on every request under test, or 404s because its path parameters
were built wrong, returns a body with no slug in it and passes — having proved
nothing. ``EXPECTED_STATUS`` states the answer each (method, path, slug) is
supposed to produce, so a case that never reached its handler is a failure and
not a pass. That is also why the write routes are driven with real request
bodies rather than empty ones: a 422 from Pydantic is a body the handler never
produced.

**Error responses are in scope, not just 200s.** The 403 a viewer gets on a
write and the 404 an unknown slug gets are the most likely places for a slug to
end up — an error message is the thing a developer interpolates a parameter into
"for debuggability", and it is the response most likely to be pasted into a
ticket or shipped to an error-reporting service. Both are driven on every route
that has them.

``test_slug_access.py`` §6 stays where it is. It covers the dependency in
isolation, which is a different claim from this one, and it keeps working if
every route in the app is deleted.

**The v2 public reads are in scope too** (section 4, ``t-am-v2-trip-reads``,
Entry 29 obligation 11). Under ``/api/v2/trips`` a non-member can read a public
trip, so every response there is checked for more than slugs: no username, no
user id and not the string ``email`` either (contract, "Never in any response a
non-member can receive"). Same rules as above: the route set comes from the live
app, every case asserts its status first, and headers are searched as well as
the body.
"""

from __future__ import annotations

import io
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededBike, SeededTrip, SignedInAccount, make_async_client
from httpx import AsyncClient, Response
from jpeg_fixtures import minimal_jpeg
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_route_dependency_audit import _api_routes

from app.data import tables
from app.data.db import get_session

# A slug no trip has. Fixed rather than random, matching test_slug_access.py, so
# a failure message names the intent instead of a token.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# The three answers the contract distinguishes, as status codes, per route
# shape. Reads take either slug; writes take the rider slug only; a slug nothing
# resolves to is a 404 on both (docs/api-contract.md, "Access control: 403 and
# 404 are different answers").
READ = {"rider": HTTPStatus.OK, "viewer": HTTPStatus.OK, "unknown": HTTPStatus.NOT_FOUND}
CREATE = {
    "rider": HTTPStatus.CREATED,
    "viewer": HTTPStatus.FORBIDDEN,
    "unknown": HTTPStatus.NOT_FOUND,
}
UPDATE = {"rider": HTTPStatus.OK, "viewer": HTTPStatus.FORBIDDEN, "unknown": HTTPStatus.NOT_FOUND}

# What each registered route is supposed to answer, per slug. Keyed by (method,
# path) with the path exactly as FastAPI registers it, so the coverage test
# below can compare this table against the live route tree by equality — a route
# renamed, added or dropped shows up as a missing or stray key rather than as
# silence.
#
# HEAD is listed alongside its GET sibling rather than skipped. It is a separate
# route object running the same handler (see test_head_method.py), so it is a
# separate chance to leak; its body is thin or absent by protocol, which is why
# the assertion covers response *headers* too and not only the body.
EXPECTED_STATUS: dict[tuple[str, str], dict[str, HTTPStatus]] = {
    ("GET", "/api/trips/{slug}"): READ,
    ("HEAD", "/api/trips/{slug}"): READ,
    ("GET", "/api/trips/{slug}/stops"): READ,
    ("HEAD", "/api/trips/{slug}/stops"): READ,
    ("POST", "/api/trips/{slug}/stops"): CREATE,
    ("GET", "/api/trips/{slug}/stops/{stop_id}/photos"): READ,
    ("HEAD", "/api/trips/{slug}/stops/{stop_id}/photos"): READ,
    ("POST", "/api/trips/{slug}/stops/{stop_id}/photos"): CREATE,
    ("POST", "/api/trips/{slug}/bikes"): CREATE,
    ("PATCH", "/api/trips/{slug}/bikes/{id}"): UPDATE,
    ("GET", "/api/trips/{slug}/map"): READ,
    ("HEAD", "/api/trips/{slug}/map"): READ,
}

# Every (method, path, slug kind) this module drives, built from the live route
# tree rather than from EXPECTED_STATUS, so an unplanned route reaches the
# parametrisation and fails loudly on the lookup instead of being absent from it.
TRIP_ROUTES = sorted(
    {(method, route.path) for method, route in _api_routes() if route.path.startswith("/api/trips")}
)

CASES = [
    (method, path, kind) for method, path in TRIP_ROUTES for kind in ("rider", "viewer", "unknown")
]

# The routes that write to object storage and so need the bucket to exist. Only
# these request the `s3_bucket` fixture: every other case runs with MinIO down
# (t-slug-audit-minio-overrequest). A new route with a storage leg that is not
# added here fails with a missing-bucket error on its first run, not silently.
STORAGE_ROUTES = {("POST", "/api/trips/{slug}/stops/{stop_id}/photos")}

# The create routes — the only ones taking a client-generated id, and so the
# only ones with a 200 replay and a 409 conflict body to leak-check
# (t-slug-audit-replay-and-conflict-bodies). Derived from the plan, not listed.
CREATE_ROUTES = sorted(key for key, row in EXPECTED_STATUS.items() if row is CREATE)

# The methods that go through the write gate and so need a session (Entry 29).
WRITE_METHODS = {"POST", "PATCH"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededStop:
    """A stop row this module put on the first seeded trip, for the photo routes."""

    id: str
    trip_id: str


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real application — not a probe app — talking to the test database."""
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
async def seeded_stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[SeededStop]:
    """
    One stop on the first seeded trip, so the photo routes have a real ``{stop_id}``.

    Inserted straight into the table rather than created through
    ``POST /stops``: a fixture built by calling the code under audit would make
    every photo-route case depend on the stop endpoint still working, and would
    turn one broken handler into a cascade of failures pointing at the wrong
    file. Rows go away with the trip — ``stops.trip_id`` is ``ON DELETE
    CASCADE``, and so is ``photos.stop_id``, which is what cleans up the photo
    the upload case creates.
    """
    stop = SeededStop(id=f"test-stop-{secrets.token_urlsafe(8)}", trip_id=seeded_trips[0].id)

    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop.id,
                trip_id=stop.trip_id,
                name="Daly Waters Pub",
                lat=-16.2545,
                lng=133.3706,
                location_source="gps",
                arrived_at=datetime(2026, 6, 14, 10, 0, tzinfo=UTC),
                notes=None,
            )
        )

    try:
        yield stop
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop.id))


@pytest.fixture
async def other_trip_stop(
    migrated_engine: AsyncEngine, seeded_trips: list[SeededTrip]
) -> AsyncIterator[SeededStop]:
    """
    A stop on the *second* seeded trip — the other parent a photo id can conflict on.

    On another trip rather than a second stop on the same one, so the 409 is
    evaluated against a row belonging to a different trip: the one case where a
    request made with this trip's slug touches another trip's data.
    """
    stop = SeededStop(id=f"test-stop-{secrets.token_urlsafe(8)}", trip_id=seeded_trips[1].id)

    async with migrated_engine.begin() as conn:
        await conn.execute(
            tables.stops.insert().values(
                id=stop.id,
                trip_id=stop.trip_id,
                name="Mataranka Springs",
                lat=-14.9230,
                lng=133.1340,
                location_source="manual",
                arrived_at=datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
                notes=None,
            )
        )

    try:
        yield stop
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.stops.delete().where(tables.stops.c.id == stop.id))


@pytest.fixture
def bucket_if_storage_route(request: pytest.FixtureRequest) -> None:
    """
    ``s3_bucket``, for the parametrised cases whose route is in ``STORAGE_ROUTES`` only.

    With MinIO down, only those cases error at setup — not every case in the
    module (t-slug-audit-minio-overrequest). A fixture rather than a
    ``getfixturevalue`` in the test body so the outage still reports as a setup
    *error*, not as a failure of the leak check; ``pytest.param`` cannot carry
    ``usefixtures`` (pytest rejects it at collection — checked on 9.1.1).
    """
    params = request.node.callspec.params
    if (params["method"], params["path"]) in STORAGE_ROUTES:
        request.getfixturevalue("s3_bucket")


# --------------------------------------------------------------------------
# Driving a route
# --------------------------------------------------------------------------


def build_request(
    method: str, path: str, slug: str, stop_id: str, bike_id: str, record_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    """
    A real URL and a real request body for one (method, path).

    Bodies are valid on purpose. An empty POST would be answered 422 by Pydantic
    before the handler ever ran, and a 422 envelope contains no slug for the same
    reason an unsent request does — which is the vacuity this module's status
    assertions exist to rule out. Ids are fresh per call so a create is a create
    (201) and not an idempotent replay (200) — unless ``record_id`` pins one,
    which is how the replay and conflict cases reach their 200 and 409.
    """
    url = path.format(slug=slug, stop_id=stop_id, id=bike_id)
    new_id = record_id or str(uuid4())

    if path.endswith("/stops") and method == "POST":
        return url, {
            "json": {
                "id": new_id,
                "name": "Larrimah",
                "lat": -15.5787,
                "lng": 133.2137,
                "locationSource": "gps",
                "arrivedAt": "2026-06-15T14:30:00+09:30",
                "notes": "Pink panther out the front.",
            }
        }

    if path.endswith("/photos") and method == "POST":
        return url, {
            "data": {
                "id": new_id,
                "uploadedBy": "Alex",
                "takenAt": "2026-06-15T14:35:00+09:30",
            },
            "files": {"file": ("photo.jpg", io.BytesIO(minimal_jpeg()), "image/jpeg")},
        }

    if path.endswith("/bikes") and method == "POST":
        return url, {
            "json": {
                "id": new_id,
                "riderName": "Alex",
                "make": "Yamaha",
                "model": "Tenere 700",
                "year": 2022,
                "specs": "Rally tower, 23L tank",
            }
        }

    if method == "PATCH":
        return url, {"json": {"specs": "New tyres fitted in Katherine."}}

    return url, {}


def find_leaked_slug(response: Response, slugs: dict[str, str]) -> str | None:
    """
    The name of the first fixture slug found in the response, or ``None``.

    Headers are searched as well as the body. A ``Location``, an ``ETag`` derived
    from the request, or any hand-rolled diagnostic header carries a value into
    exactly the same places a body does — browser devtools, proxy logs, an
    error-reporting payload — so "not in the JSON" is not the guarantee worth
    holding. Returns which slug rather than a bool so the failure message can say
    whether the *write* credential escaped.
    """
    haystack = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    return next((name for name, value in slugs.items() if value in haystack), None)


# --------------------------------------------------------------------------
# 1. The audit itself
# --------------------------------------------------------------------------


def test_every_registered_trip_route_has_a_plan() -> None:
    """
    ``EXPECTED_STATUS`` and the live route tree describe the same set of routes.

    This is the one way this module can quietly stop auditing: a route lands, no
    entry is added here, and the parametrised cases below keep passing because
    the new route was never among them. Compared as an equality, so a stale entry
    for a route that no longer exists fails too — a plan describing a deleted
    route is a plan nobody is maintaining.
    """
    assert TRIP_ROUTES, "route enumeration found no /api/trips routes — the walk is broken"
    assert set(TRIP_ROUTES) == set(EXPECTED_STATUS), (
        f"registered but unplanned: {sorted(set(TRIP_ROUTES) - set(EXPECTED_STATUS))}; "
        f"planned but not registered: {sorted(set(EXPECTED_STATUS) - set(TRIP_ROUTES))}"
    )


def test_the_plan_drives_success_and_both_error_answers() -> None:
    """
    The cases span 2xx, 403 and 404 — the leak surfaces are not all one shape.

    A suite that only ever drove happy paths would never see the two responses a
    slug is most likely to be interpolated into: the "you may not write this"
    message and the "no such trip" message.
    """
    answered = {status for row in EXPECTED_STATUS.values() for status in row.values()}
    assert HTTPStatus.FORBIDDEN in answered, "no viewer-slug-on-write 403 is driven"
    assert HTTPStatus.NOT_FOUND in answered, "no unknown-slug 404 is driven"
    assert {HTTPStatus.OK, HTTPStatus.CREATED} <= answered, "no successful handler body is driven"


def test_the_leak_check_catches_a_leak() -> None:
    """
    ``find_leaked_slug`` fails on a body that contains a slug — the detector's own guard.

    Every assertion below is "the helper found nothing". If the helper could
    never find anything — a typo in the haystack, an empty ``slugs`` map — the
    whole module would be green and blind, which is the failure mode this file
    exists to prevent elsewhere. Both halves of the haystack are checked, since
    the header half has no other coverage: no route sets a header a slug could
    legitimately reach today, so nothing else would notice it going missing.
    """
    slugs = {"riderSlug of trip A": "s3cr3t-rider-token"}

    body_leak = Response(200, text='{"trip": {"riderSlug": "s3cr3t-rider-token"}}')
    header_leak = Response(200, headers={"location": "/t/s3cr3t-rider-token"}, text="{}")
    clean = Response(200, text='{"id": "trip-1", "access": "viewer"}')

    assert find_leaked_slug(body_leak, slugs) == "riderSlug of trip A"
    assert find_leaked_slug(header_leak, slugs) == "riderSlug of trip A"
    assert find_leaked_slug(clean, slugs) is None


# --------------------------------------------------------------------------
# 2. Every route, every slug
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "slug_kind"),
    CASES,
    ids=[f"{method} {path} [{kind}]" for method, path, kind in CASES],
)
async def test_no_slug_value_comes_back_from_any_route(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
    seeded_stop: SeededStop,
    bucket_if_storage_route: None,
    rider_session: SignedInAccount,
    non_member_session: SignedInAccount,
    method: str,
    path: str,
    slug_kind: str,
) -> None:
    """
    Drive the route for real, then assert no slug *value* came back.

    The status assertion comes first and is not a formality: it is what
    distinguishes "the handler ran and returned a clean body" from "the request
    never got that far". Both trips' slugs are searched for, not just the one
    used — a handler that reached the wrong row, or that listed trips, would leak
    a credential belonging to someone who is not even in this request.
    """
    trip, other = seeded_trips
    slug = {"rider": trip.rider_slug, "viewer": trip.viewer_slug, "unknown": UNKNOWN_SLUG}[
        slug_kind
    ]

    url, kwargs = build_request(method, path, slug, seeded_stop.id, seeded_bikes[0].id)
    # Since decision-log Entry 29 (t-am-write-gate-legacy) a slug only locates the
    # trip and a write needs an active member's session. Writes go as
    # `rider_session`; the viewer-slug write goes as a signed-in non-member, so
    # its planned 403 (now the membership gate's) is still the answer driven.
    # Reads need no session and send none.
    if method in WRITE_METHODS:
        account = non_member_session if slug_kind == "viewer" else rider_session
        kwargs = {**kwargs, "headers": account.headers}
    response = await client.request(method, url, **kwargs)

    expected = EXPECTED_STATUS[method, path][slug_kind]
    assert response.status_code == expected, (
        f"{method} {path} with the {slug_kind} slug answered {response.status_code}, "
        f"expected {int(expected)} — this case proves nothing about slug leakage until "
        f"it reaches the response it is supposed to. Body: {response.text[:300]!r}"
    )

    leaked = find_leaked_slug(
        response,
        {
            "rider slug (the WRITE credential)": trip.rider_slug,
            "viewer slug": trip.viewer_slug,
            "another trip's rider slug (the WRITE credential)": other.rider_slug,
            "another trip's viewer slug": other.viewer_slug,
        },
    )
    assert leaked is None, (
        f"{method} {path} answered the {slug_kind} slug with a body or header containing "
        f"the {leaked}. A slug is the credential — it cannot be rotated without re-issuing "
        f"every link. Response: {response.text[:300]!r}"
    )


# --------------------------------------------------------------------------
# 3. The two create answers the fresh-id cases never reach: replay and conflict
# --------------------------------------------------------------------------


def test_every_create_route_has_a_replay_and_conflict_case() -> None:
    """
    The replay/conflict cases below cover all three create endpoints the contract names.

    ``CREATE_ROUTES`` is derived from the plan, so a derivation that went empty
    would parametrise nothing and pass; the contract fixes the number at three
    (stops, photos, bikes — ``docs/api-contract.md``, 409 row).
    """
    assert len(CREATE_ROUTES) == 3, CREATE_ROUTES


@pytest.mark.parametrize(
    ("method", "path"),
    CREATE_ROUTES,
    ids=[f"{method} {path}" for method, path in CREATE_ROUTES],
)
async def test_no_slug_value_comes_back_from_a_replay_or_a_conflict(
    client: AsyncClient,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
    seeded_stop: SeededStop,
    other_trip_stop: SeededStop,
    bucket_if_storage_route: None,
    rider_session: SignedInAccount,
    method: str,
    path: str,
) -> None:
    """
    The 200-replay body and the 409 body are leak-checked too.

    Every case above mints a fresh id, so each create is a 201 and these two
    responses — both in the contract — were never produced. One pinned id:
    created on the first trip (201), sent again to the same parent (the 200
    replay, which returns the *stored* record, a different code path from the
    create), then sent under the *second* trip (the 409, the one response in the
    app that a request about one trip evaluates against another trip's row).
    Each status is asserted before its body, for the same fail-open reason as
    above.
    """
    trip, other = seeded_trips
    record_id = str(uuid4())
    slugs = {
        "rider slug (the WRITE credential)": trip.rider_slug,
        "viewer slug": trip.viewer_slug,
        "another trip's rider slug (the WRITE credential)": other.rider_slug,
        "another trip's viewer slug": other.viewer_slug,
    }

    async def send(slug: str, stop_id: str) -> Response:
        url, kwargs = build_request(
            method, path, slug, stop_id, seeded_bikes[0].id, record_id=record_id
        )
        # As an active member of both seeded trips (Entry 29: writes need one).
        return await client.request(method, url, **kwargs, headers=rider_session.headers)

    created = await send(trip.rider_slug, seeded_stop.id)
    assert created.status_code == HTTPStatus.CREATED, created.text[:300]

    for label, response, expected in (
        ("replay", await send(trip.rider_slug, seeded_stop.id), HTTPStatus.OK),
        ("conflict", await send(other.rider_slug, other_trip_stop.id), HTTPStatus.CONFLICT),
    ):
        assert response.status_code == expected, (
            f"{method} {path} {label} answered {response.status_code}, expected {int(expected)} — "
            f"this case proves nothing until it reaches that response. Body: {response.text[:300]!r}"
        )
        leaked = find_leaked_slug(response, slugs)
        assert leaked is None, (
            f"{method} {path} {label} ({int(expected)}) returned a body or header containing "
            f"the {leaked}. Response: {response.text[:300]!r}"
        )


# --------------------------------------------------------------------------
# 4. The v2 public reads: no slug, username, user id or "email" (obligation 11)
# --------------------------------------------------------------------------

# What each v2 GET/HEAD under /api/v2/trips answers on the *public* trip, and on
# the *private* one for a non-member. Compared against the live route tree by
# equality, as `EXPECTED_STATUS` is, so a v2 read added later must be planned
# here before it can pass. `None` = the route takes no trip (the list).
V2_READ = {"public": HTTPStatus.OK, "private": HTTPStatus.NOT_FOUND}
V2_EXPECTED_STATUS: dict[tuple[str, str], dict[str, HTTPStatus] | None] = {
    ("GET", "/api/v2/trips"): None,
    ("HEAD", "/api/v2/trips"): None,
    **{
        (method, f"/api/v2/trips/{{tripId}}{suffix}"): V2_READ
        for method in ("GET", "HEAD")
        for suffix in ("", "/bikes", "/stops", "/stops/{stopId}/photos", "/map")
    },
}

# The member list (t-am-trip-leadership) is not a public read: only an active
# member gets it, and then it carries user ids by design (`MemberOut.userId`).
# So a non-member's answer is its gate's refusal, and a rider's 200 is searched
# for everything except user ids. Planned here so the equality check still holds.
V2_MEMBER_READS = {
    ("GET", "/api/v2/trips/{tripId}/members"),
    ("HEAD", "/api/v2/trips/{tripId}/members"),
}
V2_MEMBER_READ_STATUS = {
    ("anonymous", "public"): HTTPStatus.UNAUTHORIZED,
    ("anonymous", "private"): HTTPStatus.UNAUTHORIZED,
    ("non_member", "public"): HTTPStatus.FORBIDDEN,
    ("non_member", "private"): HTTPStatus.NOT_FOUND,
}

# The trip's join-request list (t-am-join-leader) is a leader read: everyone
# here, the rider included, gets its gate's refusal, which must carry no
# identity or slug either. A leader's 200 (user ids by design, never a
# username) is covered by the access matrix and the join-request tests.
V2_LEADER_READS = {
    ("GET", "/api/v2/trips/{tripId}/join-requests"),
    ("HEAD", "/api/v2/trips/{tripId}/join-requests"),
}
V2_LEADER_READ_STATUS = {
    **V2_MEMBER_READ_STATUS,
    ("rider", "public"): HTTPStatus.FORBIDDEN,
    ("rider", "private"): HTTPStatus.FORBIDDEN,
}

V2_ROUTES = sorted(
    {
        (method, route.path)
        for method, route in _api_routes()
        if route.path.startswith("/api/v2/trips") and method in {"GET", "HEAD"}
    }
)

# The callers a public v2 response can reach. `rider` is a member of both seeded
# trips, so its responses are the full, undelayed ones: those must not carry
# anyone's identity either.
V2_IDENTITIES = ("anonymous", "non_member", "rider")

V2_CASES = [
    (method, path, visibility, identity)
    for method, path in V2_ROUTES
    for visibility in ("public", "private")
    for identity in V2_IDENTITIES
    # The list takes no trip, so one visibility covers it.
    if not (path == "/api/v2/trips" and visibility == "private")
]


@dataclass(frozen=True, slots=True)
class V2World:
    """Trip 0 public with a visible stop and photo; trip 1 private with its own stop."""

    stops: dict[str, str]  # visibility -> a stop id on that trip


@pytest.fixture
async def v2_world(
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    seeded_bikes: list[SeededBike],
) -> V2World:
    """
    Seeded trip 0 made public, with a stop old enough to be visible and a photo on it.

    Straight into the tables, like ``seeded_stop``. Seeded trip 1 stays private
    (the migration default) and gets a stop too, so its 404s are driven with a
    real stop id. Everything goes with the trips (``ON DELETE CASCADE``).
    """
    public, private = seeded_trips
    stops = {"public": f"v2-stop-{uuid4()}", "private": f"v2-stop-{uuid4()}"}
    arrived = datetime.now(UTC) - timedelta(hours=48)
    async with migrated_engine.begin() as conn:
        await conn.execute(
            update(tables.trips).where(tables.trips.c.id == public.id).values(visibility="public")
        )
        for visibility, trip in (("public", public), ("private", private)):
            await conn.execute(
                tables.stops.insert().values(
                    id=stops[visibility],
                    trip_id=trip.id,
                    name="Tennant Creek",
                    lat=-19.6480,
                    lng=134.1910,
                    location_source="gps",
                    arrived_at=arrived,
                )
            )
        await conn.execute(
            tables.photos.insert().values(
                id=str(uuid4()),
                stop_id=stops["public"],
                object_key=f"{public.id}/v2-leak-check.jpg",
                uploaded_by="Test Rider",
                taken_at=arrived,
            )
        )
    return V2World(stops=stops)


def find_leaked_value(response: Response, secrets_: dict[str, str]) -> str | None:
    """
    The name of the first value found in the response, body or headers, or ``None``.

    Case-insensitive, so ``email`` catches ``Email`` and ``EMAIL`` too.
    """
    haystack = (
        response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    ).lower()
    return next((name for name, value in secrets_.items() if value.lower() in haystack), None)


def test_the_v2_leak_check_catches_each_kind_of_leak() -> None:
    """The detector finds a username, a user id and ``email``, in a body and in a header."""
    secrets_ = {"username": "tabc123", "user id": "u-1", "the string email": "email"}

    assert find_leaked_value(Response(200, text='{"by": "tabc123"}'), secrets_) == "username"
    assert find_leaked_value(Response(200, headers={"x-u": "u-1"}, text="{}"), secrets_) == (
        "user id"
    )
    assert find_leaked_value(Response(200, text='{"Email": null}'), secrets_) == (
        "the string email"
    )
    assert find_leaked_value(Response(200, text='{"id": "trip-1"}'), secrets_) is None


def test_every_v2_read_has_a_plan() -> None:
    """``V2_EXPECTED_STATUS`` and the live v2 GET/HEAD routes are the same set."""
    assert V2_ROUTES, "route enumeration found no /api/v2/trips reads — the walk is broken"
    planned = set(V2_EXPECTED_STATUS) | V2_MEMBER_READS | V2_LEADER_READS
    assert set(V2_ROUTES) == planned, (
        f"registered but unplanned: {sorted(set(V2_ROUTES) - planned)}; "
        f"planned but not registered: {sorted(planned - set(V2_ROUTES))}"
    )


@pytest.mark.parametrize(
    ("method", "path", "visibility", "identity"),
    V2_CASES,
    ids=[f"{m} {p} [{v}, {i}]" for m, p, v, i in V2_CASES],
)
async def test_no_identity_or_slug_comes_back_from_a_v2_read(
    client: AsyncClient,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    v2_world: V2World,
    rider_session: SignedInAccount,
    non_member_session: SignedInAccount,
    method: str,
    path: str,
    visibility: str,
    identity: str,
) -> None:
    """
    Drive the v2 read for real, assert its status, then search the whole response.

    Searched for: both trips' slugs (the seeded trips have them, as pre-0003
    trips do), both accounts' usernames and user ids, and ``email``. The
    private trip is driven as a non-member (its 404) and as a member (its 200).
    """
    trip = seeded_trips[0] if visibility == "public" else seeded_trips[1]
    url = path.format(tripId=trip.id, stopId=v2_world.stops[visibility])
    account = {"non_member": non_member_session, "rider": rider_session}.get(identity)
    headers = account.headers if account is not None else {}

    response = await client.request(method, url, headers=headers)

    member_read = (method, path) in V2_MEMBER_READS
    leader_read = (method, path) in V2_LEADER_READS
    plan = None if member_read or leader_read else V2_EXPECTED_STATUS[method, path]
    if leader_read:
        expected = V2_LEADER_READ_STATUS[identity, visibility]
    elif identity == "rider" or (plan is None and not member_read):
        expected = HTTPStatus.OK
    elif member_read:
        expected = V2_MEMBER_READ_STATUS[identity, visibility]
    else:
        expected = plan[visibility]
    assert response.status_code == expected, (
        f"{method} {url} as {identity} answered {response.status_code}, expected "
        f"{int(expected)} — this case proves nothing until it reaches that response. "
        f"Body: {response.text[:300]!r}"
    )

    accounts = {"rider": rider_session, "non-member": non_member_session}
    async with migrated_engine.connect() as conn:
        usernames = {
            row.id: row.username
            for row in await conn.execute(
                select(tables.users.c.id, tables.users.c.username).where(
                    tables.users.c.id.in_([a.user_id for a in accounts.values()])
                )
            )
        }
    other = seeded_trips[1] if visibility == "public" else seeded_trips[0]
    leaked = find_leaked_value(
        response,
        {
            "rider slug": trip.rider_slug,
            "viewer slug": trip.viewer_slug,
            "another trip's rider slug": other.rider_slug,
            "another trip's viewer slug": other.viewer_slug,
            # A member list hands an active member the members' user ids by
            # design; a non-member's id must still never appear in it.
            **{
                f"{name}'s user id": a.user_id
                for name, a in accounts.items()
                if not (member_read and identity == "rider" and name == "rider")
            },
            **{f"{name}'s username": usernames[a.user_id] for name, a in accounts.items()},
            "the string email": "email",
        },
    )
    assert leaked is None, (
        f"{method} {url} as {identity} returned a body or header containing {leaked}. "
        f"Response: {response.text[:300]!r}"
    )
