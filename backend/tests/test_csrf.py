"""
CSRF middleware (``core/csrf.py``) -- task ``t-am-csrf``, contract obligation 8.

**Written from the contract, not from the middleware.** Every assertion comes
from ``docs/api-contract.md`` ("CSRF", "Error envelope") and the task's AC:

- Every ``POST``, ``PUT``, ``PATCH`` and ``DELETE`` whose path starts with
  ``/api`` is checked.
- If ``Sec-Fetch-Site`` is present it must be ``same-origin`` or ``none``.
- If it is absent, ``Origin`` must be present and its host and port must equal
  the ``Host`` header. The scheme is ignored. ``Origin: null`` fails.
- A failure is ``403 FORBIDDEN`` with "This request came from another site and
  was blocked.", carrying the same security headers as any other response.
- The ``403`` is middleware-level: reachable on every unsafe ``/api`` path,
  whether or not a route would have matched (it is never listed per endpoint,
  like ``405``). So the probes cover a real route, a real path with the wrong
  method, and a path no route has.
- ``GET``, ``HEAD`` and ``OPTIONS`` are never checked.

Cases the contract is silent on (``TRACE``, duplicated ``Origin`` headers) are
tested fail-closed and labelled *implementation-defined* in their docstrings.

The default factory client sends ``Origin: https://testserver`` and
``Host: testserver``. Requests that must carry *no* ``Origin`` (or two) are
built as a raw ``httpx.Request`` and sent through the factory client with
``client.send`` -- which does not merge the client's default headers -- and the
test asserts the header set actually sent before looking at the answer.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from conftest import SeededTrip, make_async_client
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.data import tables
from app.data.db import get_session
from app.models.common import ErrorCode, ErrorEnvelope

CSRF_MESSAGE = "This request came from another site and was blocked."

UNSAFE = ["POST", "PUT", "PATCH", "DELETE"]
SAFE = ["GET", "HEAD", "OPTIONS"]

# The baseline security headers every response carries (test_security_headers.py).
SECURITY_HEADERS = [
    "x-content-type-options",
    "referrer-policy",
    "x-frame-options",
    "content-security-policy",
]

UNKNOWN_SLUG = "csrf-probe-no-such-slug-0000"

# (method, path, status when the CSRF check lets it through). The pass status
# proves the request reached routing rather than just "was not 403".
#   - bikes create / bike patch are real write routes; an unknown slug is 404.
#   - PUT / DELETE on the bikes path: the path exists, the method doesn't -> 405.
#   - /api/health exists for GET only -> 405 for every unsafe method.
#   - /api/csrf-probe-no-route: no route at all -> 404.
PROBES: list[tuple[str, str, int]] = [
    ("POST", f"/api/trips/{UNKNOWN_SLUG}/bikes", 404),
    ("PATCH", f"/api/trips/{UNKNOWN_SLUG}/bikes/some-bike-id", 404),
    ("PUT", f"/api/trips/{UNKNOWN_SLUG}/bikes", 405),
    ("DELETE", f"/api/trips/{UNKNOWN_SLUG}/bikes", 405),
    *[(m, "/api/health", 405) for m in UNSAFE],
    *[(m, "/api/csrf-probe-no-route", 404) for m in UNSAFE],
]
PROBE_IDS = [f"{m} {p}" for m, p, _ in PROBES]


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
async def client(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The real application (middleware included), on this test's database and loop."""
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


async def send_raw(
    client: AsyncClient,
    method: str,
    path: str,
    headers: list[tuple[str, str]],
    json_body: Any = None,
) -> httpx.Response:
    """
    Send exactly ``headers`` (plus httpx's automatic ``Host``) -- no client defaults.

    Asserts the ``Origin`` headers on the wire are exactly the ones asked for,
    so a test for "Origin absent" can't pass on a request that had one.
    """
    kwargs: dict[str, Any] = {"headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    url = client.build_request("GET", path).url
    request = httpx.Request(method, url, **kwargs)
    wanted = [v for k, v in headers if k.lower() == "origin"]
    assert request.headers.get_list("origin") == wanted, request.headers
    return await client.send(request)


def assert_csrf_forbidden(response: httpx.Response, reference: httpx.Response) -> None:
    """403 with the FORBIDDEN envelope, the contract message and the security headers."""
    assert response.status_code == 403, (response.status_code, response.text)
    assert response.headers["content-type"].startswith("application/json")
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code == ErrorCode.FORBIDDEN
    assert envelope.error.message == CSRF_MESSAGE
    for name in SECURITY_HEADERS:
        assert reference.headers.get(name), f"reference response lacks {name}"
        assert response.headers.get(name) == reference.headers.get(name), name


def assert_not_csrf_blocked(response: httpx.Response, expected_status: int) -> None:
    """The request reached routing: its status is the route's answer, not the CSRF 403."""
    assert response.status_code == expected_status, (response.status_code, response.text)
    if response.status_code >= 400 and response.content:
        assert response.json()["error"]["message"] != CSRF_MESSAGE


@pytest.fixture
async def reference(client: AsyncClient) -> httpx.Response:
    """An ordinary response, whose security headers the CSRF 403 must match."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    return response


# --------------------------------------------------------------------------
# 1. Sec-Fetch-Site present
# --------------------------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_cross_or_same_site_is_forbidden(
    client: AsyncClient,
    reference: httpx.Response,
    method: str,
    path: str,
    pass_status: int,
    site: str,
) -> None:
    """Contract rule 1: only same-origin / none pass. The factory's matching Origin is sent too."""
    response = await client.request(method, path, headers={"Sec-Fetch-Site": site})
    assert_csrf_forbidden(response, reference)


@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_cross_site_with_matching_origin_is_still_forbidden(
    client: AsyncClient, reference: httpx.Response, method: str, path: str, pass_status: int
) -> None:
    """When Sec-Fetch-Site is present it decides; a matching Origin can't rescue it."""
    response = await send_raw(
        client,
        method,
        path,
        [("Origin", "https://testserver"), ("Sec-Fetch-Site", "cross-site")],
    )
    assert_csrf_forbidden(response, reference)


@pytest.mark.parametrize("site", ["same-origin", "none"])
@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_same_origin_or_none_passes(
    client: AsyncClient, method: str, path: str, pass_status: int, site: str
) -> None:
    response = await client.request(method, path, headers={"Sec-Fetch-Site": site})
    assert_not_csrf_blocked(response, pass_status)


@pytest.mark.parametrize("site", ["same-origin", "none"])
@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_allowed_sec_fetch_site_decides_without_origin(
    client: AsyncClient, method: str, path: str, pass_status: int, site: str
) -> None:
    """Rule 2 (Origin) applies only when Sec-Fetch-Site is absent."""
    response = await send_raw(client, method, path, [("Sec-Fetch-Site", site)])
    assert_not_csrf_blocked(response, pass_status)


@pytest.mark.parametrize("site", ["cross-origin", "bogus", "", "Cross-Site"])
@pytest.mark.parametrize("method", UNSAFE)
async def test_unrecognised_sec_fetch_site_value_is_forbidden(
    client: AsyncClient, reference: httpx.Response, method: str, site: str
) -> None:
    """Contract rule 1: a present header that isn't same-origin / none fails."""
    response = await send_raw(
        client,
        method,
        "/api/health",
        [("Origin", "https://testserver"), ("Sec-Fetch-Site", site)],
    )
    assert_csrf_forbidden(response, reference)


# --------------------------------------------------------------------------
# 2. Sec-Fetch-Site absent -- Origin vs Host
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_missing_origin_is_forbidden(
    client: AsyncClient, reference: httpx.Response, method: str, path: str, pass_status: int
) -> None:
    response = await send_raw(client, method, path, [])
    assert "sec-fetch-site" not in response.request.headers
    assert_csrf_forbidden(response, reference)


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://evil.example",
        "https://testserver.evil.example",
        "https://evil-testserver",
        "https://testserver:8443",
        "http://testserver:8000",
        "not a url",
        "",
    ],
)
@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_mismatched_origin_is_forbidden(
    client: AsyncClient,
    reference: httpx.Response,
    method: str,
    path: str,
    pass_status: int,
    origin: str,
) -> None:
    """``null``, a foreign host, a lookalike host, a port mismatch or garbage all fail."""
    response = await send_raw(client, method, path, [("Origin", origin)])
    assert_csrf_forbidden(response, reference)


@pytest.mark.parametrize(("method", "path", "pass_status"), PROBES, ids=PROBE_IDS)
async def test_matching_origin_passes(
    client: AsyncClient, method: str, path: str, pass_status: int
) -> None:
    response = await send_raw(client, method, path, [("Origin", "https://testserver")])
    assert_not_csrf_blocked(response, pass_status)


@pytest.mark.parametrize("method", UNSAFE)
async def test_origin_scheme_is_ignored(client: AsyncClient, method: str) -> None:
    """TLS terminates at the ingress, so ``http://`` vs ``https://`` doesn't matter."""
    response = await send_raw(client, method, "/api/health", [("Origin", "http://testserver")])
    assert_not_csrf_blocked(response, 405)


@pytest.mark.parametrize("method", UNSAFE)
async def test_explicit_port_must_match_host_port(method: str) -> None:
    """Host ``testserver:8443``: the same port passes, the default port does not."""
    import app.main

    async with make_async_client(app.main.app, base_url="https://testserver:8443") as c:
        ok = await send_raw(c, method, "/api/health", [("Origin", "https://testserver:8443")])
        assert ok.request.headers["host"] == "testserver:8443"
        assert_not_csrf_blocked(ok, 405)

        reference = await c.get("/api/health")
        blocked = await send_raw(c, method, "/api/health", [("Origin", "https://testserver")])
        assert_csrf_forbidden(blocked, reference)


# --------------------------------------------------------------------------
# 3. Safe methods are never checked
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", SAFE)
@pytest.mark.parametrize(
    "headers",
    [
        [("Sec-Fetch-Site", "cross-site")],
        [("Origin", "https://evil.example"), ("Sec-Fetch-Site", "cross-site")],
        [("Origin", "null")],
        [],
    ],
    ids=["cross-site", "cross-site+foreign-origin", "origin-null", "no-headers"],
)
@pytest.mark.parametrize("path", ["/api/health", "/api/csrf-probe-no-route"])
async def test_safe_methods_are_never_blocked(
    client: AsyncClient, method: str, headers: list[tuple[str, str]], path: str
) -> None:
    response = await send_raw(client, method, path, headers)
    assert response.status_code != 403, (method, path, response.text)
    if response.content and response.status_code >= 400:
        assert response.json()["error"]["message"] != CSRF_MESSAGE


# --------------------------------------------------------------------------
# 4. Edge cases the contract is silent on -- fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origins",
    [
        ["https://testserver", "https://testserver"],
        ["https://testserver", "https://evil.example"],
        ["https://evil.example", "https://testserver"],
    ],
    ids=["same-twice", "good-then-evil", "evil-then-good"],
)
@pytest.mark.parametrize("method", UNSAFE)
async def test_duplicated_origin_header_is_forbidden(
    client: AsyncClient, reference: httpx.Response, method: str, origins: list[str]
) -> None:
    """
    Implementation-defined: the contract says nothing about more than one Origin.

    A browser never sends two, so any request carrying two was not built by the
    SPA; failing closed means no choice of "which one wins" can be exploited.
    """
    response = await send_raw(client, method, "/api/health", [("Origin", o) for o in origins])
    assert_csrf_forbidden(response, reference)


@pytest.mark.parametrize(
    "headers",
    [[("Sec-Fetch-Site", "cross-site")], [], [("Origin", "https://evil.example")]],
    ids=["cross-site", "no-origin", "foreign-origin"],
)
async def test_trace_cross_site_is_forbidden(
    client: AsyncClient, reference: httpx.Response, headers: list[tuple[str, str]]
) -> None:
    """
    Implementation-defined: the contract lists POST/PUT/PATCH/DELETE and "GET,
    HEAD and OPTIONS are never checked", but is silent on TRACE. Anything not
    known-safe is treated as unsafe (fail closed).
    """
    response = await send_raw(client, "TRACE", "/api/health", headers)
    assert_csrf_forbidden(response, reference)


# --------------------------------------------------------------------------
# 5. A blocked request writes nothing
# --------------------------------------------------------------------------


@pytest.fixture
async def created_ids(migrated_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    ids: list[str] = []
    try:
        yield ids
    finally:
        async with migrated_engine.begin() as conn:
            await conn.execute(tables.bikes.delete().where(tables.bikes.c.id.in_(ids)))


async def bike_count(engine: AsyncEngine, trip_id: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count()).select_from(tables.bikes).where(tables.bikes.c.trip_id == trip_id)
        )
        return int(result.scalar_one())


def bike_payload() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "riderName": f"CSRF {secrets.token_hex(4)}",
        "make": "Yamaha",
        "model": "Tenere 700",
        "year": 2022,
        "specs": "",
    }


@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", "https://testserver"), ("Sec-Fetch-Site", "cross-site")],
        [("Sec-Fetch-Site", "same-site")],
        [],
        [("Origin", "null")],
        [("Origin", "https://evil.example")],
    ],
    ids=["cross-site", "same-site", "no-origin", "origin-null", "foreign-origin"],
)
async def test_blocked_create_writes_nothing(
    client: AsyncClient,
    reference: httpx.Response,
    migrated_engine: AsyncEngine,
    seeded_trips: list[SeededTrip],
    created_ids: list[str],
    headers: list[tuple[str, str]],
) -> None:
    """A valid rider-slug bike create, blocked by CSRF, leaves the table untouched."""
    trip = seeded_trips[0]
    path = f"/api/trips/{trip.rider_slug}/bikes"
    payload = bike_payload()
    created_ids.append(payload["id"])

    before = await bike_count(migrated_engine, trip.id)
    response = await send_raw(client, "POST", path, headers, json_body=payload)
    assert_csrf_forbidden(response, reference)
    assert await bike_count(migrated_engine, trip.id) == before

    # Control: the identical request from the same origin does write -- so the
    # unchanged count above is the CSRF check, not a payload the route rejects.
    allowed = await send_raw(
        client, "POST", path, [("Origin", "https://testserver")], json_body=payload
    )
    assert allowed.status_code == 201, allowed.text
    assert await bike_count(migrated_engine, trip.id) == before + 1
