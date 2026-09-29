"""
Slug access-control tests — the 403/404 boundary the whole app sits behind.

``app/core/security.py`` is the only thing standing between a stranger with a
guessed URL and a trip: there are no accounts, no passwords and no second
factor, so spec Section 12 ranks this the top-priority test area. What is under
test is not "does a lookup work" but the three answers the contract
distinguishes (``docs/api-contract.md``, "Access control: 403 and 404 are
different answers"):

- rider slug -> the trip, acting as a rider;
- viewer slug -> the trip on a read, **403** on a write;
- anything else -> **404**, on reads and writes alike.

Both directions of collapsing 403 and 404 are bugs with victims. A 404 on a
viewer-slug write tells a legitimate read-only guest their link is broken. A 403
on an unresolvable slug tells someone guessing links that they can tell "wrong
slug" from "right slug, wrong permission", which is the signal the unguessable
-slug model exists to withhold. Every assertion below asserts the status and the
code *separately*, because a response that swapped one for the other would
satisfy a combined assertion on either half.

Three deliberate choices about how this is set up:

1. **Probe routes, not the real app** — the same pattern as
   ``tests/test_error_envelope.py``. None of the eight contract endpoints is
   wired yet, so a throwaway ``FastAPI()`` carries one read route and one write
   route that exist only in this file. They go through the real dependency, the
   real repository and the real exception layer, so what is asserted is the
   response a client would actually receive, not an exception object.
2. **A real database.** The dependency's entire job is a query, so a stubbed
   repository would test the ``if`` statements and nothing about whether the
   right trip comes back. Rows come from the ``seeded_trips`` fixture, which
   inserts and removes its own.
3. **``get_session`` is overridden onto the test's own engine**, not because the
   real one is wrong but because the app-level engine is created at import time
   and pools connections; reused from a different event loop in a later test, an
   asyncpg connection fails for reasons that have nothing to do with access
   control. ``test_dependencies_take_a_session_from_get_session`` asserts
   separately that the real dependency does declare ``Depends(get_session)`` —
   otherwise the override could be pointed at a function nothing uses and every
   test below would still pass.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import date
from http import HTTPStatus
from pathlib import Path as FilePath
from typing import Annotated, Any, get_args

import pytest
from conftest import SeededTrip, make_async_client
from fastapi import Depends, FastAPI, params
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.errors import ApiError, register_exception_handlers
from app.core.security import (
    TripContext,
    access_for_slug,
    require_rider_access,
    require_trip_access,
)
from app.data.db import get_session
from app.data.repositories.trips import TripRecord, get_by_slug
from app.models.common import ErrorCode, ErrorDetail, ErrorEnvelope
from app.models.trip import Access

READ_PATH = "/probe/trips/{slug}"
WRITE_PATH = "/probe/trips/{slug}/stops"

# A slug no trip has. Fixed rather than random: an unknown slug must be answered
# identically however it was produced, and a literal makes the test's own intent
# obvious in a failure message.
UNKNOWN_SLUG = "definitely-not-a-real-slug-0000"

# The same thing, but unrepresentable in a Postgres `text` parameter. Written
# percent-encoded because that is how it arrives over the wire — a client sends
# `/api/trips/abc%00def`, the server percent-decodes it, and the path parameter
# the dependency receives is `"abc\x00def"` with a real NUL in it. Asserted
# through the URL rather than by calling the dependency with a NUL string, so
# the test covers the route a caller can actually reach.
NUL_SLUG_ENCODED = "abc%00def"
NUL_SLUG_DECODED = "abc\x00def"


def build_probe_app() -> FastAPI:
    """
    A disposable app with one read route and one write route.

    They are the smallest thing that can exercise a dependency end to end: each
    declares nothing but the guard, and echoes back what the guard resolved.
    Nothing is added to ``app/`` for testing.

    What they return is modelled on the real handlers — the trip's id and the
    derived access, and pointedly *not* either slug, since ``TripOut`` has no
    slug fields either.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get(READ_PATH)
    async def probe_read(
        context: Annotated[TripContext, Depends(require_trip_access)],
    ) -> dict[str, str]:
        return {"id": context.trip.id, "name": context.trip.name, "access": context.access.value}

    @app.post(WRITE_PATH)
    async def probe_write(
        context: Annotated[TripContext, Depends(require_rider_access)],
    ) -> dict[str, str]:
        return {"id": context.trip.id, "name": context.trip.name, "access": context.access.value}

    return app


@pytest.fixture
async def probe_client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The probe app, talking to the migrated test database in this test's event loop."""
    app = build_probe_app()
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = session_override

    async with make_async_client(app) as client:
        yield client


@pytest.fixture
async def db_session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A bare session for the repository tests, which do not go through HTTP."""
    sessionmaker = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session


def assert_envelope(response: Any) -> ErrorDetail:
    """
    Validate an error body against ``ErrorEnvelope`` and return its detail.

    Parsed through the contract model rather than poked at as a dict, so the
    check is "is this the shape the generated client expects" and not "does this
    happen to contain the keys I looked for".
    """
    assert response.headers["content-type"] == "application/json"
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty — it is shown to a rider"
    return envelope.error


# --------------------------------------------------------------------------
# 1. The rider slug gets through, as a rider
# --------------------------------------------------------------------------


async def test_rider_slug_on_write_is_allowed(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """The rider's own link can write to their own trip — the baseline."""
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.rider_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == trip.id


async def test_rider_slug_on_write_resolves_rider_access(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """...and the context it hands the handler says ``rider``, which drives the write UI."""
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.rider_slug))

    assert response.json()["access"] == Access.RIDER.value


# --------------------------------------------------------------------------
# 2. The viewer slug on a write — 403, and specifically not 404
# --------------------------------------------------------------------------


async def test_viewer_slug_on_write_is_forbidden_status(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """A read-only link may not write."""
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.viewer_slug))

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert_envelope(response)


async def test_viewer_slug_on_write_is_forbidden_code(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """...and reports FORBIDDEN, asserted apart from the status.

    The offline queue branches on ``code``, not on the status line, to decide
    whether a queued write can ever succeed — so the code is its own assertion.
    """
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.viewer_slug))

    assert assert_envelope(response).code is ErrorCode.FORBIDDEN


async def test_viewer_slug_on_write_is_not_a_not_found(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    The collapse this must never make: 404 for a viewer-slug write.

    Stated as its own negative assertion rather than left implied by the two
    above, because this is the half that harms a legitimate user — a guest told
    "not found" concludes the link they were sent is broken and asks for a new
    one, which is how a rider ends up handing out their *write* link.
    """
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.viewer_slug))

    assert response.status_code != HTTPStatus.NOT_FOUND
    assert assert_envelope(response).code is not ErrorCode.NOT_FOUND


async def test_viewer_slug_on_write_message_is_rider_readable(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    The message explains the situation to a human, without naming internals.

    ``ErrorDetail.message`` is contractually safe to display, so this is the
    text a guest actually reads. It must say the link is read-only rather than
    surface a stack frame, a column name or a SQL fragment.
    """
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.viewer_slug))

    message = assert_envelope(response).message
    assert "read-only" in message.lower()
    for internal in ("Traceback", "viewer_slug", "rider_slug", "SELECT", "ApiError"):
        assert internal not in message


async def test_viewer_slug_write_body_has_exactly_the_contract_keys(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """The 403 body is the envelope and nothing else — extras are contract drift."""
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=trip.viewer_slug))

    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}


# --------------------------------------------------------------------------
# 3. An unknown slug — 404, and specifically not 403
# --------------------------------------------------------------------------


async def test_unknown_slug_on_write_is_not_found_status(probe_client: AsyncClient) -> None:
    """A slug no trip has is a 404 even on a write endpoint."""
    response = await probe_client.post(WRITE_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


async def test_unknown_slug_on_write_is_not_found_code(probe_client: AsyncClient) -> None:
    """...and reports NOT_FOUND, asserted apart from the status."""
    response = await probe_client.post(WRITE_PATH.format(slug=UNKNOWN_SLUG))

    assert assert_envelope(response).code is ErrorCode.NOT_FOUND


async def test_unknown_slug_on_write_is_not_forbidden(probe_client: AsyncClient) -> None:
    """
    The other collapse, and the one with the worse consequence.

    A 403 for a slug that resolves to nothing is an oracle: it tells whoever is
    trying links that this one named a real trip, separating "no such trip" from
    "real trip, wrong permission". The access model is a random token and
    nothing else, so that distinction is the only thing a guesser needs.
    """
    response = await probe_client.post(WRITE_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code != HTTPStatus.FORBIDDEN
    assert assert_envelope(response).code is not ErrorCode.FORBIDDEN


async def test_unknown_slug_on_read_is_not_found_status(probe_client: AsyncClient) -> None:
    """The read dependency answers the same way — an unknown slug is a 404."""
    response = await probe_client.get(READ_PATH.format(slug=UNKNOWN_SLUG))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


async def test_unknown_slug_on_read_is_not_found_code(probe_client: AsyncClient) -> None:
    """...with NOT_FOUND, asserted apart from the status."""
    response = await probe_client.get(READ_PATH.format(slug=UNKNOWN_SLUG))

    assert assert_envelope(response).code is ErrorCode.NOT_FOUND


async def test_unknown_slug_answers_identically_on_read_and_write(
    probe_client: AsyncClient,
) -> None:
    """
    Both dependencies give a stranger the exact same answer.

    If the read path said 404 and the write path said something else, the pair
    of responses would be the oracle even though neither response alone looked
    wrong. Sameness across endpoints is part of the guarantee, not a detail of
    either one.
    """
    read = await probe_client.get(READ_PATH.format(slug=UNKNOWN_SLUG))
    write = await probe_client.post(WRITE_PATH.format(slug=UNKNOWN_SLUG))

    assert read.status_code == write.status_code
    assert assert_envelope(read).code is assert_envelope(write).code
    assert assert_envelope(read).message == assert_envelope(write).message


# --------------------------------------------------------------------------
# 4. Reads accept both slugs, and report which one was used
# --------------------------------------------------------------------------


async def test_read_with_rider_slug_succeeds(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """Either slug reads; the rider's is one of them."""
    trip = seeded_trips[0]

    response = await probe_client.get(READ_PATH.format(slug=trip.rider_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == trip.id


async def test_read_with_rider_slug_reports_rider_access(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """...and is reported as rider access."""
    trip = seeded_trips[0]

    response = await probe_client.get(READ_PATH.format(slug=trip.rider_slug))

    assert response.json()["access"] == Access.RIDER.value


async def test_read_with_viewer_slug_succeeds(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """A viewer link reads the trip — being read-only is not being locked out."""
    trip = seeded_trips[0]

    response = await probe_client.get(READ_PATH.format(slug=trip.viewer_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == trip.id


async def test_read_with_viewer_slug_reports_viewer_access(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    ...and is reported as **viewer** access.

    Asserted separately from the 403 on writes on purpose. The same fact is
    encoded twice — as the guard that rejects the write, and as the flag the
    frontend renders write UI from — and they fail independently. An ``access``
    stuck at ``rider`` would show a guest an Add stop button that always errors,
    with the enforcement still perfectly correct.
    """
    trip = seeded_trips[0]

    response = await probe_client.get(READ_PATH.format(slug=trip.viewer_slug))

    assert response.json()["access"] == Access.VIEWER.value


# --------------------------------------------------------------------------
# 5. The right trip — with two trips in the table
# --------------------------------------------------------------------------
# Every test above would pass against a lookup that ignored its slug entirely
# and returned whatever row came back first, because with one trip in the table
# "the first row" and "the right row" are the same row.


async def test_read_resolves_the_second_trip_not_the_first(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """Trip B's viewer slug resolves to trip B — never to trip A."""
    first, second = seeded_trips

    response = await probe_client.get(READ_PATH.format(slug=second.viewer_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == second.id
    assert response.json()["id"] != first.id


async def test_write_resolves_the_second_trip_not_the_first(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """The same on the write path: trip B's rider slug writes to trip B."""
    first, second = seeded_trips

    response = await probe_client.post(WRITE_PATH.format(slug=second.rider_slug))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == second.id
    assert response.json()["id"] != first.id


async def test_one_trips_viewer_slug_cannot_write_to_the_other(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    Trip B's viewer slug is still 403 while trip A sits in the table.

    The cross-trip version of the permission check: a lookup that matched the
    slug but then read the permission off the wrong row would let a guest of one
    trip write to another.
    """
    _, second = seeded_trips

    response = await probe_client.post(WRITE_PATH.format(slug=second.viewer_slug))

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert assert_envelope(response).code is ErrorCode.FORBIDDEN


# --------------------------------------------------------------------------
# 6. No slug ever comes back in a response
# --------------------------------------------------------------------------


async def test_no_slug_appears_in_any_response_body(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    Neither slug is echoed by any answer these dependencies produce.

    A slug is the credential. Reflecting one into a body puts it wherever the
    body ends up — a log line, a screenshot in a bug report, a browser cache, an
    error-reporting service — and unlike a password it cannot be rotated without
    re-sending the link to everyone. Asserted against the raw text, because a
    leak into an unexpected field would still be a leak.
    """
    first, second = seeded_trips
    responses = [
        await probe_client.get(READ_PATH.format(slug=first.rider_slug)),
        await probe_client.get(READ_PATH.format(slug=first.viewer_slug)),
        await probe_client.post(WRITE_PATH.format(slug=first.rider_slug)),
        await probe_client.post(WRITE_PATH.format(slug=first.viewer_slug)),
        await probe_client.post(WRITE_PATH.format(slug=UNKNOWN_SLUG)),
        await probe_client.get(READ_PATH.format(slug=UNKNOWN_SLUG)),
        await probe_client.get(READ_PATH.format(slug=second.viewer_slug)),
    ]

    for response in responses:
        for slug in (
            first.rider_slug,
            first.viewer_slug,
            second.rider_slug,
            second.viewer_slug,
            UNKNOWN_SLUG,
        ):
            assert slug not in response.text, f"{slug!r} leaked into {response.request.url}"


# --------------------------------------------------------------------------
# 7. Access derivation, apart from enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("which", "expected"),
    [("rider_slug", Access.RIDER), ("viewer_slug", Access.VIEWER)],
)
def test_access_is_derived_from_the_matched_record(which: str, expected: Access) -> None:
    """
    ``access_for_slug`` reads the record's own columns, so it is testable alone.

    No database and no request: the derivation is a pure comparison, and keeping
    it separable is what lets a bug in the *flag* fail a test even when the
    *guard* is still rejecting writes correctly.
    """
    record = TripRecord(
        id="trip-1",
        name="Test trip",
        start_date=date(2026, 6, 1),
        rider_slug="rider-token",
        viewer_slug="viewer-token",
    )

    assert access_for_slug(getattr(record, which), record) is expected


def test_access_defaults_to_viewer_for_an_unrelated_string() -> None:
    """Anything that is not the rider slug is a viewer — the lesser permission wins."""
    record = TripRecord(
        id="trip-1",
        name="Test trip",
        start_date=date(2026, 6, 1),
        rider_slug="rider-token",
        viewer_slug="viewer-token",
    )

    assert access_for_slug("something-else", record) is Access.VIEWER


# --------------------------------------------------------------------------
# 8. The repository underneath
# --------------------------------------------------------------------------


async def test_repository_finds_a_trip_by_rider_slug(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """The lookup matches on the rider column."""
    trip = seeded_trips[0]

    record = await get_by_slug(db_session, trip.rider_slug)

    assert record is not None
    assert record.id == trip.id


async def test_repository_finds_a_trip_by_viewer_slug(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """
    ...and on the viewer column, which is the half a one-sided WHERE would drop.

    A query narrowed to ``rider_slug`` alone turns every viewer link in
    existence into a 404 — the guests are exactly the users who would never be
    able to report it as anything but "the link you sent me is broken".
    """
    trip = seeded_trips[0]

    record = await get_by_slug(db_session, trip.viewer_slug)

    assert record is not None
    assert record.id == trip.id


async def test_repository_returns_none_for_an_unknown_slug(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """
    No match is ``None``, not an exception.

    Whether "nothing found" is a 404 is a transport decision; the repository is
    callable from a background job that has no HTTP response to produce.
    """
    assert await get_by_slug(db_session, UNKNOWN_SLUG) is None


async def test_repository_carries_both_slugs_and_the_trip_fields(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """
    The record carries both slugs, whichever one was used to find it.

    That is what makes access derivable from the row rather than from which
    branch of the ``OR`` fired — without both, the caller would have to be told
    which column matched.
    """
    trip = seeded_trips[1]

    record = await get_by_slug(db_session, trip.viewer_slug)

    assert record == TripRecord(
        id=trip.id,
        name=trip.name,
        start_date=trip.start_date,
        rider_slug=trip.rider_slug,
        viewer_slug=trip.viewer_slug,
    )


async def test_repository_does_not_raise_api_error(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """An explicit guard on the layering rule, since ``None`` alone looks accidental."""
    try:
        result = await get_by_slug(db_session, UNKNOWN_SLUG)
    except ApiError as exc:  # pragma: no cover - the failure this guards against
        pytest.fail(f"the repository raised a transport error: {exc!r}")

    assert result is None


# --------------------------------------------------------------------------
# 9. Wiring and layering, asserted structurally
# --------------------------------------------------------------------------


def _declared_dependencies(func: Any) -> list[Any]:
    """
    The callables a function declares via ``Depends`` in its own signature.

    ``eval_str=True`` because ``security.py`` uses ``from __future__ import
    annotations``: without it every annotation is a plain string and the
    ``Annotated`` metadata FastAPI reads is invisible here — which would make
    both structural tests below silently vacuous rather than failing.
    """
    found: list[Any] = []
    for parameter in inspect.signature(func, eval_str=True).parameters.values():
        for metadata in get_args(parameter.annotation)[1:]:
            if isinstance(metadata, params.Depends):
                found.append(metadata.dependency)
    return found


@pytest.mark.parametrize("dependency", [require_trip_access, require_rider_access])
def test_dependencies_take_a_session_from_get_session(dependency: Any) -> None:
    """
    Both guards get their session through ``get_session``.

    The HTTP tests override that exact callable, so without this they would keep
    passing against a dependency that had quietly started opening its own
    connection — the override would simply never fire.
    """
    assert get_session in _declared_dependencies(dependency)


@pytest.mark.parametrize("dependency", [require_trip_access, require_rider_access])
def test_dependencies_declare_the_slug_path_parameter(dependency: Any) -> None:
    """
    Both take ``slug`` with a real ``description`` — this feeds the OpenAPI spec.

    The generated frontend client is built from that spec, so a path parameter
    documented only by its type reaches every call site as an unexplained
    string.
    """
    parameters = inspect.signature(dependency, eval_str=True).parameters
    assert "slug" in parameters

    annotations = get_args(parameters["slug"].annotation)
    descriptions = [
        getattr(item, "description", None) for item in annotations[1:] if hasattr(item, "in_")
    ]
    assert descriptions, "slug is not declared as a path parameter"
    assert all(described and len(described) > 20 for described in descriptions)


def test_security_module_raises_only_through_api_error() -> None:
    """
    Every error leaves ``security.py`` as an ``ApiError``, never hand-built.

    ``app/core/errors.py`` is what guarantees the single envelope shape and the
    status/code pairing that makes 403-vs-404 unrepresentable as a mismatched
    pair (decision-log entry 7). A response built by hand here — or a bare
    framework exception carrying a code chosen by hand — would bypass both
    guarantees while looking correct from the outside. Asserted against the
    source because that is where the mistake would be made; no runtime test
    distinguishes an envelope produced properly from an identical one typed out
    by hand.
    """
    source = FilePath(__file__).resolve().parents[1] / "app" / "core" / "security.py"
    text = source.read_text(encoding="utf-8")

    for banned in ("JSONResponse", "HTTPException", '"FORBIDDEN"', "'FORBIDDEN'", "ErrorCode."):
        assert banned not in text, f"{banned} appears in security.py — raise via ApiError instead"

    assert "ApiError.forbidden(" in text
    assert "ApiError.not_found(" in text


# --------------------------------------------------------------------------
# 10. A slug Postgres cannot represent is still just an unknown slug
# --------------------------------------------------------------------------
# A NUL byte is not a legal value of a `text` parameter: asyncpg's bind is
# rejected by the server with `invalid byte sequence for encoding "UTF8": 0x00`
# before any row is considered. Left unguarded, that driver error escapes the
# repository and renders as `500 INTERNAL_ERROR`, which is wrong twice over.
#
# It is wrong about *who broke*: nothing on the server is faulty, the caller
# sent a string no trip could ever have. And it is wrong in the one field the
# offline queue makes a programmatic decision on — `code` is how a queued write
# chooses retry vs. never-retry (decision-log entry 6), and `INTERNAL_ERROR`
# means "try again later". A write queued against such a URL would retry
# forever against a request that cannot succeed on any attempt.
#
# It is also, on its own, a 403/404 leak of the shape the module docstring
# describes: a response that differs from the ordinary unknown-slug answer tells
# a prober something about how their input was handled.


async def test_nul_byte_slug_on_read_is_not_found_status(probe_client: AsyncClient) -> None:
    """`GET /trips/abc%00def` is a 404 — not a 500."""
    response = await probe_client.get(READ_PATH.format(slug=NUL_SLUG_ENCODED))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


async def test_nul_byte_slug_on_read_is_not_found_code(probe_client: AsyncClient) -> None:
    """...reporting NOT_FOUND, and specifically not INTERNAL_ERROR."""
    response = await probe_client.get(READ_PATH.format(slug=NUL_SLUG_ENCODED))

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND
    assert detail.code is not ErrorCode.INTERNAL_ERROR


async def test_nul_byte_slug_on_write_is_not_found_status(probe_client: AsyncClient) -> None:
    """The write dependency answers identically — the guard is in the shared lookup."""
    response = await probe_client.post(WRITE_PATH.format(slug=NUL_SLUG_ENCODED))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


async def test_nul_byte_slug_on_write_is_not_found_code(probe_client: AsyncClient) -> None:
    """...with NOT_FOUND, asserted apart from the status."""
    response = await probe_client.post(WRITE_PATH.format(slug=NUL_SLUG_ENCODED))

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND
    assert detail.code is not ErrorCode.INTERNAL_ERROR


async def test_nul_byte_slug_is_answered_like_any_other_unknown_slug(
    probe_client: AsyncClient,
) -> None:
    """
    Byte-for-byte the same answer as a plain unknown slug.

    Sameness is the assertion, not "some 404": if a NUL slug produced a
    *different* 404 message from an ordinary miss, the pair of responses would
    still tell a prober that their input took a different path through the
    server, which is the class of signal this model exists to withhold.
    """
    nul = await probe_client.get(READ_PATH.format(slug=NUL_SLUG_ENCODED))
    unknown = await probe_client.get(READ_PATH.format(slug=UNKNOWN_SLUG))

    assert nul.status_code == unknown.status_code
    assert assert_envelope(nul).code is assert_envelope(unknown).code
    assert assert_envelope(nul).message == assert_envelope(unknown).message


async def test_real_rider_slug_with_a_trailing_nul_does_not_resolve(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """
    The dangerous variant: a **real** rider slug with `\\x00` stuck on the end.

    This is not an unknown-slug test with a decoration. The guard returns `None`
    without asking the database, so the one way it could be written wrong is to
    strip or truncate at the NUL and look up what is left — which would make
    `<rider slug>%00` a working write credential, and one that bypasses any
    future rate limiting or logging keyed on the exact slug string. A 404 here
    is the assertion that nothing resolves, not merely that nothing crashed.
    """
    trip = seeded_trips[0]

    response = await probe_client.post(WRITE_PATH.format(slug=f"{trip.rider_slug}%00"))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.status_code != HTTPStatus.OK
    assert assert_envelope(response).code is ErrorCode.NOT_FOUND


async def test_real_viewer_slug_with_a_trailing_nul_does_not_resolve_on_read(
    probe_client: AsyncClient, seeded_trips: list[SeededTrip]
) -> None:
    """The same on the read path, where a truncating guard would leak the trip itself."""
    trip = seeded_trips[0]

    response = await probe_client.get(READ_PATH.format(slug=f"{trip.viewer_slug}%00"))

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert trip.id not in response.text


async def test_repository_returns_none_for_a_nul_containing_slug(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """
    At the repository, where the fix lives: `None`, and nothing raised.

    Asserted against bare `Exception` rather than `DBAPIError` alone, because
    the contract this holds is the module docstring's — *nothing* escapes
    `get_by_slug` except a `TripRecord` or `None`. Which driver exception a
    future SQLAlchemy or asyncpg would have raised is not the point.
    """
    for slug in (NUL_SLUG_DECODED, f"{seeded_trips[0].rider_slug}\x00", "\x00"):
        try:
            result = await get_by_slug(db_session, slug)
        # BLE001 is suppressed because the breadth *is* the assertion.
        # Narrowing to DBAPIError would re-state the current implementation
        # rather than the contract, and would let a differently-typed escape
        # through silently.
        except Exception as exc:  # noqa: BLE001 # pragma: no cover - the failure guarded against
            pytest.fail(f"get_by_slug({slug!r}) raised {exc!r} instead of returning None")

        assert result is None, f"get_by_slug({slug!r}) resolved to a trip"


async def test_nul_lookup_leaves_the_session_usable(
    db_session: AsyncSession, seeded_trips: list[SeededTrip]
) -> None:
    """
    A real slug still resolves on the same session afterwards.

    This is what distinguishes "the NUL never reached Postgres" from "the driver
    error was caught and swallowed". A rejected bind aborts the transaction the
    session is in, so a swallowed error would leave every later query on that
    session failing with `InFailedSQLTransaction` — the request would still be
    broken, just further from the cause. One session, two calls, on purpose.
    """
    trip = seeded_trips[0]

    assert await get_by_slug(db_session, NUL_SLUG_DECODED) is None

    record = await get_by_slug(db_session, trip.rider_slug)
    assert record is not None
    assert record.id == trip.id
