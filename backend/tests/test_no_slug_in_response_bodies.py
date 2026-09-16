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
"""

from __future__ import annotations

import io
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import pytest
from conftest import SeededBike, SeededTrip
from httpx import ASGITransport, AsyncClient, Response
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
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
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
async def s3_bucket() -> None:
    """The MinIO bucket the photo upload writes into — the one case with a storage leg."""
    from app.storage.s3_client import BUCKET_NAME, get_s3_client

    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
    except Exception:  # noqa: BLE001 - any failure here means "no bucket yet"
        s3.create_bucket(Bucket=BUCKET_NAME)


# --------------------------------------------------------------------------
# Driving a route
# --------------------------------------------------------------------------


def build_request(
    method: str, path: str, slug: str, stop_id: str, bike_id: str
) -> tuple[str, dict[str, Any]]:
    """
    A real URL and a real request body for one (method, path).

    Bodies are valid on purpose. An empty POST would be answered 422 by Pydantic
    before the handler ever ran, and a 422 envelope contains no slug for the same
    reason an unsent request does — which is the vacuity this module's status
    assertions exist to rule out. Ids are fresh per call so a create is a create
    (201) and not an idempotent replay (200).
    """
    url = path.format(slug=slug, stop_id=stop_id, id=bike_id)

    if path.endswith("/stops") and method == "POST":
        return url, {
            "json": {
                "id": str(uuid4()),
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
                "id": str(uuid4()),
                "uploadedBy": "Alex",
                "takenAt": "2026-06-15T14:35:00+09:30",
            },
            "files": {"file": ("photo.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg")},
        }

    if path.endswith("/bikes") and method == "POST":
        return url, {
            "json": {
                "id": str(uuid4()),
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
    s3_bucket: None,
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
