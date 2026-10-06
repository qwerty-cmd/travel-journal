"""
HEAD on the routes that serve GET — and the OpenAPI document staying clean.

HEAD is GET without a body (RFC 9110 §9.3.2): a server that registers GET is
expected to answer HEAD on the same path, so `HEAD /api/health` is a 200 and not
a 405. `t-head-on-get-routes` implements that as a **second registration of the
same handler** (`app.add_api_route(..., methods=["HEAD"], include_in_schema=False)`)
rather than `methods=["GET", "HEAD"]` on the one route, because
`fastapi.openapi.utils.get_openapi_path` loops `for method in route.methods` with
no HEAD exclusion while `operation_id` is per-*route*: one route carrying both
verbs emits a second `head:` operation into the OpenAPI document, and Kubb
generates a duplicate frontend hook from it.

Two consequences shape every test in this file:

1. **The invariant is per-path, not per-route.** The GET route's method set is
   exactly `{"GET"}`; HEAD lives on a different route object at the same path. A
   guard shaped "every route that accepts GET also accepts HEAD" would fail
   against correct code. What has to hold is: *for every path some route serves
   under GET, some route on that path answers HEAD.* Asserted here by sending a
   real HEAD request, which is also the only form that cannot be fooled by
   FastAPI representing an included router as one opaque `_IncludedRouter` entry
   with `path=None` and `methods=None` (decision-log entry 7b — a route-table
   scan keyed on `route.methods` sees `/api/health` and *none* of the contract
   endpoints, and fails silently in the direction of "nothing to check").

2. **The OpenAPI half is invisible to a passing suite.** Nothing about request
   handling changes if the document grows a `head:` operation; the damage lands
   in `frontend/src/api/` at `npx kubb generate` time, one repository away. It is
   exactly what a future "simplify these two registrations into one" edit would
   reintroduce, so the document is asserted on directly.

The path list is taken from the generated document rather than hand-written, so
the seven contract endpoints still to land are covered as they arrive — the task
note is explicit that HEAD is adopted per-endpoint rather than retrofitted in a
sweep, and a convention that relies on the next author remembering it reaches
only some of its sites (decision-log entry 5).
"""

from __future__ import annotations

import re
import warnings
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

import pytest
from conftest import SeededTrip, make_async_client
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data.db import get_session

HEALTH_PATH = "/api/health"
TRIP_PATH = "/api/trips/{slug}"

# A slug no trip has, for the paths the guard reaches generically. The guard
# asserts HEAD and GET agree, not what they agree *on*, so a 404 is a perfectly
# good answer for it to compare.
UNKNOWN_SLUG = "head-guard-no-such-slug-0000"

# Paths deliberately outside the GET -> HEAD convention, named rather than left
# to be inferred from whatever happens to be registered.
#
# The SPA fallback is GET-only by decision (task note, `t-head-on-get-routes`):
# it serves index.html for client-side deep links, and a HEAD sibling for it
# would buy nothing. It is registered only when `frontend/dist` exists, so it is
# absent from the document in a plain run — excluded by name anyway, so a run
# with a built frontend present reports a deliberate choice as such instead of
# as a failure. Both spellings, because FastAPI renders the `:path` convertor
# out of the documented path (as `test_unknown_api_catch_all_is_absent_from_the
# _openapi_spec` already relies on).
#
# FastAPI's own `/docs`, `/redoc` and `/openapi.json` need no entry: Starlette
# registers them as plain `Route`s, which carry `{"GET", "HEAD"}` already, and
# `include_in_schema=False` keeps them out of the document this guard reads.
HEAD_EXEMPT_DOC_PATHS = {"/{full_path}", "/{full_path:path}"}


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """
    The real application, talking to the test database on this test's event loop.

    Same override as ``test_trip_metadata_endpoint.py`` and for the same reason:
    the app-level engine is built at import time and pools asyncpg connections
    bound to whichever loop first used them, so a request that reaches the
    database through the production engine from a second loop dies inside the
    driver (measured: ``AttributeError: 'NoneType' object has no attribute
    'send'``) — a failure that says nothing about HEAD.
    """
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


def documented_get_paths() -> list[str]:
    """Every path the OpenAPI document says answers GET, minus the named exemptions."""
    import app.main

    paths = app.main.app.openapi()["paths"]
    return [
        path
        for path, operations in paths.items()
        if "get" in operations and path not in HEAD_EXEMPT_DOC_PATHS
    ]


def fill_path_params(path: str) -> str:
    """`/api/trips/{slug}` -> a requestable URL. Generic, so new endpoints need no edit here."""
    return re.sub(r"\{[^}]+\}", UNKNOWN_SLUG, path)


def regenerate_openapi() -> tuple[dict[str, Any], list[warnings.WarningMessage]]:
    """
    The document built fresh, with the warnings FastAPI raised while building it.

    `app.openapi()` memoises into `app.openapi_schema` and the cached copy is
    almost certainly already populated by the time this runs (other tests call
    `.openapi()`, and so does `/openapi.json`), so the cache is cleared first:
    reading the memoised document would assert on content while making the
    duplicate-operation-id warning — which is emitted during *generation* —
    structurally unobservable. Restored afterwards so the ordering of this
    module against the rest of the suite stays irrelevant.
    """
    import app.main

    application = app.main.app
    cached = application.openapi_schema
    try:
        application.openapi_schema = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            spec = application.openapi()
        return spec, list(caught)
    finally:
        application.openapi_schema = cached


# --------------------------------------------------------------------------
# 1. Behaviour — HEAD is answered, by the same handler, with no body
# --------------------------------------------------------------------------


async def test_head_health_returns_200_not_405(client: AsyncClient) -> None:
    """
    The acceptance criterion in its plainest form: `HEAD /api/health` is a 200.

    Container hosts and `docker compose` health checks send HEAD to liveness
    probes; before this task it was a 405 reconstructed by the /api catch-all.
    """
    response = await client.head(HEALTH_PATH)

    assert response.status_code == HTTPStatus.OK
    assert not response.content, "HEAD must not carry a body"


async def test_head_on_a_trip_slug_still_runs_the_access_dependency(
    client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    The HEAD registration reuses `get_trip`, so `require_trip_access` still runs.

    This is the assertion that separates "HEAD is served by the real handler"
    from "HEAD is served by *something*". A HEAD route wired to a stub — or one
    that skipped the dependency — would answer 200 for a slug no trip has, which
    leaks the existence of nothing but is also the shape in which a verb quietly
    stops being access-controlled. Both directions are asserted: a real viewer
    slug succeeds, an unknown slug is the same 404 GET gives.
    """
    trip = seeded_trips[0]

    found = await client.head(TRIP_PATH.format(slug=trip.viewer_slug))
    missing = await client.head(TRIP_PATH.format(slug=UNKNOWN_SLUG))

    assert found.status_code == HTTPStatus.OK
    assert not found.content, "HEAD must not carry a body"
    assert missing.status_code == HTTPStatus.NOT_FOUND


async def test_every_documented_get_path_also_answers_head(client: AsyncClient) -> None:
    """
    The guard that carries this convention to the seven endpoints still to land.

    Per-path, not per-route (see the module docstring): asserted by sending HEAD
    and comparing against GET on the same URL. `405` is the specific failure a
    missing HEAD registration produces — the /api catch-all matches every verb,
    derives the path's real verb set and reports "this path exists, that method
    doesn't" — but the assertion is the stronger one RFC 9110 §9.3.2 actually
    promises: identical status, no body. A HEAD route pointed at the wrong
    handler passes a `!= 405` check and fails this one.

    The non-vacuity assertion is not ceremony. This guard is only as good as its
    path list, and the list is derived; if `documented_get_paths()` ever returns
    `[]` — a renamed OpenAPI key, a document that failed to build — the loop
    below passes while checking nothing, which is decision-log entry 10 exactly
    (a test that looked like coverage and had none). Both paths in the app today
    are named, so the list shrinking is a failure rather than a quiet pass.
    """
    paths = documented_get_paths()

    assert {HEALTH_PATH, TRIP_PATH} <= set(paths), (
        f"the documented-GET-path list lost a known path, so this guard checks nothing: {paths}"
    )

    for path in paths:
        url = fill_path_params(path)
        head = await client.head(url)
        get = await client.get(url)

        assert head.status_code != HTTPStatus.METHOD_NOT_ALLOWED, (
            f"{path} answers GET but not HEAD — register the HEAD sibling "
            f'(add_api_route(..., methods=["HEAD"], include_in_schema=False))'
        )
        assert head.status_code == get.status_code, path
        assert not head.content, f"{path} returned a body for HEAD"


# --------------------------------------------------------------------------
# 2. The OpenAPI document — the half no request can observe
# --------------------------------------------------------------------------


def test_openapi_document_declares_no_head_operation() -> None:
    """
    AC8: the HEAD siblings stay out of the document Kubb generates from.

    `include_in_schema=False` on the HEAD registration is what buys this, and a
    reader simplifying the two registrations into `methods=["GET", "HEAD"]` gets
    a working app, a green suite and a second `head:` operation per path —
    surfacing as a duplicate hook in `frontend/src/api/`, in a different
    repository, at generate time.

    Asserted over every documented path rather than the two that exist today, so
    an endpoint that lands with the naive declaration fails here rather than in
    the frontend build.
    """
    spec, _ = regenerate_openapi()
    paths = spec["paths"]

    assert {HEALTH_PATH, TRIP_PATH} <= set(paths), f"document lost a known path: {list(paths)}"

    for path, operations in paths.items():
        assert "head" not in operations, (
            f"{path} declares a `head:` operation — Kubb will generate a duplicate hook. "
            "Register HEAD as a separate route with include_in_schema=False."
        )


def test_generating_the_openapi_document_emits_no_duplicate_operation_id_warning() -> None:
    """
    The same regression seen from the generator's side.

    `operation_id` is per-route, so a route carrying both verbs yields one id for
    two operations and FastAPI warns `Duplicate Operation ID ...`. That warning
    is the signal a human would notice at generate time; this asserts it is
    silent today, and asserts operation ids are unique in the document itself so
    the check does not depend on FastAPI keeping the warning text (or the warning
    at all) in a future release.
    """
    spec, caught = regenerate_openapi()

    duplicates = [str(w.message) for w in caught if "Duplicate Operation ID" in str(w.message)]
    assert not duplicates, duplicates

    operation_ids = [
        operation["operationId"]
        for operations in spec["paths"].values()
        for operation in operations.values()
        if "operationId" in operation
    ]
    assert operation_ids, "no operation ids in the document — this check would pass vacuously"
    assert len(operation_ids) == len(set(operation_ids)), operation_ids
