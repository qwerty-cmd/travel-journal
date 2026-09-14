"""
Error envelope tests — does every non-2xx response actually come back as
``ErrorEnvelope``?

The contract (``docs/api-contract.md``, "Error envelope") promises one shape for
every failure, so that the generated client — and especially the offline queue,
which decides retry-vs-never-retry from the body alone — has a single parsing
path. ``app/models/common.py`` only declares that shape. What these tests are
protecting is the *wiring* in ``app/core/errors.py``: the failures nobody writes
code for (FastAPI's ``{"detail": [...]}`` 422, Starlette's routing 404 and 405,
an unhandled exception's 500) are precisely the ones that would otherwise ship a
second response shape.

Two deliberate choices:

1. **Probe routes, not the real app.** None of the eight contract endpoints
   exists yet, so there is nothing to make fail. Asserting that a handler is
   *registered* would prove nothing about what it returns, so the tests build a
   throwaway ``FastAPI()`` here, call the same ``register_exception_handlers`` on
   it, and hang routes off it that exist only in this file. Nothing in ``app/``
   is added for testing. The real app is checked separately, at the bottom, for
   registration only.
2. **No database, no Docker.** Everything here is in-process. If this module
   ever starts needing the ``postgres`` container, the exception layer has grown
   a dependency it should not have.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.errors import (
    INTERNAL_ERROR_MESSAGE,
    ApiError,
    api_error_handler,
    http_exception_handler,
    register_exception_handlers,
    unhandled_exception_handler,
    validation_error_handler,
)
from app.main import _ALL_METHODS as ALL_API_METHODS
from app.models.common import ErrorCode, ErrorDetail, ErrorEnvelope

# A string that exists nowhere else in the codebase, so finding it in a response
# body can only mean the 500 handler leaked the exception it was given.
LEAK_CANARY = "SECRET-DB-HOST-12345"

# The shape a driver error actually arrives in once `data/` starts wrapping one.
# Used for the `ApiError.internal` leak check, which is the path that goes live
# the moment application code raises a 500 of its own rather than crashing.
DB_ERROR_TEXT = f'FATAL: password authentication failed for user "postgres" host={LEAK_CANARY}'


class ProbeBody(BaseModel):
    """Body for the validation probe — one required int, enough to fail on."""

    distance_km: int


def build_probe_app() -> FastAPI:
    """
    A disposable app carrying the same exception layer as the real one.

    The routes below are test fixtures, not contract endpoints: their only job is
    to raise each class of failure the handlers claim to normalise.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/probe/validate")
    async def probe_validate(body: ProbeBody) -> dict[str, int]:
        return {"distance_km": body.distance_km}

    @app.get("/probe/api-error")
    async def probe_api_error(status: int, code: ErrorCode, message: str) -> None:
        raise ApiError(status_code=status, code=code, message=message)

    @app.get("/probe/api-error-internal")
    async def probe_api_error_internal() -> None:
        raise ApiError.internal(DB_ERROR_TEXT)

    @app.get("/probe/http-exception")
    async def probe_http_exception(status: int) -> None:
        raise StarletteHTTPException(status_code=status)

    @app.get("/probe/http-exception-headers")
    async def probe_http_exception_headers(status: int) -> None:
        raise StarletteHTTPException(
            status_code=status,
            detail="Nope.",
            headers={"Allow": "GET, HEAD", "X-Probe": "kept"},
        )

    @app.get("/probe/boom")
    async def probe_boom() -> None:
        raise RuntimeError(f"connection to host={LEAK_CANARY} user=postgres failed")

    @app.get("/probe/get-only")
    async def probe_get_only() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Client that raises server exceptions — the default, for everything but the 500 test."""
    return TestClient(build_probe_app())


@pytest.fixture(scope="module")
def quiet_client() -> TestClient:
    """
    Client that returns the 500 instead of re-raising.

    ``TestClient`` re-raises unhandled exceptions by default, which would hide
    the response a real HTTP client receives — and the response is the thing
    under test.
    """
    return TestClient(build_probe_app(), raise_server_exceptions=False)


def assert_envelope(response: Any) -> ErrorDetail:
    """
    Validate a response body against ``ErrorEnvelope`` and return its detail.

    Every error-body assertion in this module goes through here, so the check is
    always "does this parse as the contract model", never "does this dict happen
    to contain the keys I looked for". Content type is asserted here too: a body
    the client can't parse as JSON fails the contract regardless of its shape.
    """
    assert response.headers["content-type"] == "application/json"
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.message, "message must be non-empty — it is shown to a rider"
    return envelope.error


def find_key(payload: Any, key: str) -> bool:
    """True if ``key`` appears anywhere in a nested structure, at any depth."""
    if isinstance(payload, dict):
        return key in payload or any(find_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(find_key(item, key) for item in payload)
    return False


# --------------------------------------------------------------------------
# 1. FastAPI's own 422
# --------------------------------------------------------------------------


def test_validation_error_returns_envelope(client: TestClient) -> None:
    """A body that fails schema validation comes back as VALIDATION_ERROR, not `detail`."""
    response = client.post("/probe/validate", json={"distance_km": "not-a-number"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.VALIDATION_ERROR


def test_validation_error_has_no_detail_key_at_any_level(client: TestClient) -> None:
    """
    FastAPI's ``{"detail": [...]}`` must be gone entirely, not merely wrapped.

    A leftover ``detail`` anywhere in the body would mean the generated client
    still sees two possible shapes for a 422.
    """
    response = client.post("/probe/validate", json={})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert not find_key(body, "detail")
    assert_envelope(response)


def test_validation_error_body_has_exactly_the_contract_keys(client: TestClient) -> None:
    """The envelope carries these keys and no others — extras are contract drift."""
    response = client.post("/probe/validate", json={"distance_km": None})

    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}
    assert_envelope(response)


# --------------------------------------------------------------------------
# 2. ApiError — what core/security.py will raise
# --------------------------------------------------------------------------


def test_api_error_forbidden_status(client: TestClient) -> None:
    """A FORBIDDEN ApiError responds 403."""
    response = client.get(
        "/probe/api-error",
        params={"status": 403, "code": "FORBIDDEN", "message": "This link is read-only."},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert_envelope(response)


def test_api_error_forbidden_code(client: TestClient) -> None:
    """...and carries FORBIDDEN, asserted apart from the status.

    Status and code are checked separately on purpose: a handler that returned
    403/NOT_FOUND or 404/FORBIDDEN would satisfy a single combined assertion on
    either half. The contract distinguishes "you may not" from "it isn't there",
    and both directions of that collapse have to be able to fail a test.
    """
    response = client.get(
        "/probe/api-error",
        params={"status": 403, "code": "FORBIDDEN", "message": "This link is read-only."},
    )

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.FORBIDDEN


def test_api_error_not_found_status(client: TestClient) -> None:
    """A NOT_FOUND ApiError responds 404."""
    response = client.get(
        "/probe/api-error",
        params={"status": 404, "code": "NOT_FOUND", "message": "No trip with that link."},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


def test_api_error_not_found_code(client: TestClient) -> None:
    """...and carries NOT_FOUND, asserted apart from the status (see above)."""
    response = client.get(
        "/probe/api-error",
        params={"status": 404, "code": "NOT_FOUND", "message": "No trip with that link."},
    )

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND


def test_api_error_message_is_verbatim(client: TestClient) -> None:
    """The message reaches the rider exactly as the call site wrote it."""
    message = "This link is read-only — ask the rider for the editing link."
    response = client.get(
        "/probe/api-error",
        params={"status": 403, "code": "FORBIDDEN", "message": message},
    )

    detail = assert_envelope(response)
    assert detail.message == message


# --------------------------------------------------------------------------
# 2a. ApiError's INTERNAL_ERROR leak boundary
# --------------------------------------------------------------------------
# `ApiError` is documented as the single exception application code raises, so
# it is a third door into the envelope alongside the 500 handler and the
# HTTPException handler. Nothing raises an INTERNAL_ERROR through it *today* —
# which is the point: the first caller will be `data/` or `security.py` wrapping
# a driver error, and a driver error's text is a database host and password.


def test_api_error_internal_keeps_status_and_code(client: TestClient) -> None:
    """`ApiError.internal` is still a 500 INTERNAL_ERROR — the code is not softened."""
    response = client.get("/probe/api-error-internal")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.INTERNAL_ERROR


def test_api_error_internal_message_is_the_fixed_generic_string(client: TestClient) -> None:
    """
    ...but its message is replaced, exactly as on the other two paths.

    The contract binds INTERNAL_ERROR to a fixed string regardless of which
    handler produced the response. A message that varies with the fault is a
    message the rider can read the database host out of.
    """
    response = client.get("/probe/api-error-internal")

    detail = assert_envelope(response)
    assert detail.message == INTERNAL_ERROR_MESSAGE


def test_api_error_internal_does_not_leak_its_message(client: TestClient) -> None:
    """
    The text handed to `ApiError.internal` appears nowhere in the response.

    Asserted against the raw body, not the parsed model: a leak into an
    unexpected extra field would still be a leak.
    """
    response = client.get("/probe/api-error-internal")

    assert LEAK_CANARY not in response.text
    assert "password authentication failed" not in response.text
    assert "postgres" not in response.text
    assert_envelope(response)


def test_api_error_internal_is_logged_server_side(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Withholding the real message from the rider only works if it is kept."""
    with caplog.at_level("ERROR", logger="app.core.errors"):
        client.get("/probe/api-error-internal")

    assert any(LEAK_CANARY in record.getMessage() for record in caplog.records)


def test_api_error_non_internal_message_still_reaches_the_rider(client: TestClient) -> None:
    """
    The generic-message rule applies to INTERNAL_ERROR *only*.

    Without this, replacing every message would pass the leak tests above while
    destroying the four codes whose whole job is to explain a condition the
    caller is allowed to know about.
    """
    response = client.get(
        "/probe/api-error",
        params={"status": 404, "code": "NOT_FOUND", "message": "No trip with that link."},
    )

    detail = assert_envelope(response)
    assert detail.message == "No trip with that link."
    assert detail.message != INTERNAL_ERROR_MESSAGE


# --------------------------------------------------------------------------
# 2b. ApiError cannot be built with a status and code that disagree
# --------------------------------------------------------------------------
# 403-vs-404 is spec Section 12's top-priority distinction and decision-log
# entry 6 calls the code load-bearing (the offline queue branches on it). A
# transposed pair at a call site — `ApiError(404, FORBIDDEN, ...)` — would put a
# contract-violating body on the wire that neither a status assertion nor a code
# assertion alone would catch.


@pytest.mark.parametrize(
    ("factory", "expected_status", "expected_code"),
    [
        (ApiError.forbidden, HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN),
        (ApiError.not_found, HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND),
        (ApiError.validation, HTTPStatus.UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR),
        (ApiError.internal, HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_classmethod_pairs_status_with_the_contract_code(
    factory: Any, expected_status: HTTPStatus, expected_code: ErrorCode
) -> None:
    """Each documented constructor produces the pairing the contract's table specifies."""
    error = factory("something happened")

    assert error.status_code == expected_status
    assert error.code is expected_code


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, ErrorCode.FORBIDDEN),  # the transposed pair, both directions
        (403, ErrorCode.NOT_FOUND),
        (422, ErrorCode.NOT_FOUND),
        (500, ErrorCode.VALIDATION_ERROR),
        (200, ErrorCode.NOT_FOUND),
    ],
)
def test_direct_construction_rejects_a_contradicting_code(status: int, code: ErrorCode) -> None:
    """The `__init__` backstop refuses a pair the status->code mapping disagrees with."""
    with pytest.raises(ValueError):
        ApiError(status_code=status, code=code, message="nope")


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (422, ErrorCode.VALIDATION_ERROR),
        (500, ErrorCode.INTERNAL_ERROR),
        (409, ErrorCode.INTERNAL_ERROR),  # unnamed status -> INTERNAL_ERROR, per the mapping
    ],
)
def test_direct_construction_still_allows_every_contract_pair(
    status: int, code: ErrorCode
) -> None:
    """
    The backstop rejects contradictions, not legitimate use.

    A guard that refused a pair the contract allows would be worse than none: it
    would push call sites back onto raising bare `HTTPException`s.
    """
    error = ApiError(status_code=status, code=code, message="fine")

    assert error.status_code == status
    assert error.code is code


# --------------------------------------------------------------------------
# 3. StarletteHTTPException -> the status/code mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (422, ErrorCode.VALIDATION_ERROR),
        (409, ErrorCode.INTERNAL_ERROR),
        (500, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_http_exception_maps_to_contract_code(
    client: TestClient, status: int, expected_code: ErrorCode
) -> None:
    """Every raised HTTPException lands on the code the contract pairs with its status."""
    response = client.get("/probe/http-exception", params={"status": status})

    assert response.status_code == status
    detail = assert_envelope(response)
    assert detail.code is expected_code


def test_http_exception_403_status(client: TestClient) -> None:
    """403 keeps its status..."""
    response = client.get("/probe/http-exception", params={"status": 403})

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert_envelope(response)


def test_http_exception_403_code(client: TestClient) -> None:
    """...and maps to FORBIDDEN, not NOT_FOUND."""
    response = client.get("/probe/http-exception", params={"status": 403})

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.FORBIDDEN


def test_http_exception_404_status(client: TestClient) -> None:
    """404 keeps its status..."""
    response = client.get("/probe/http-exception", params={"status": 404})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert_envelope(response)


def test_http_exception_404_code(client: TestClient) -> None:
    """...and maps to NOT_FOUND, not FORBIDDEN."""
    response = client.get("/probe/http-exception", params={"status": 404})

    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND


def test_wrong_method_returns_method_not_allowed_envelope(client: TestClient) -> None:
    """
    Starlette's own 405 is normalised.

    Nothing in application code raises this — the router does, before any handler
    runs — so it is exactly the kind of response that would otherwise escape the
    envelope.
    """
    response = client.post("/probe/get-only")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.METHOD_NOT_ALLOWED


def test_router_405_still_carries_allow(client: TestClient) -> None:
    """
    Starlette's routing 405 keeps its `Allow` header through our handler.

    RFC 9110 §15.5.6 makes `Allow` a MUST on a 405, and FastAPI's default
    handler sends it. Replacing that handler to normalise the *body* must not
    quietly drop the header that tells the client which verbs would work.
    """
    response = client.post("/probe/get-only")

    assert "GET" in response.headers.get("allow", "")


def test_http_exception_headers_are_propagated(client: TestClient) -> None:
    """
    Any `HTTPException.headers` survive, not only `Allow`.

    `WWW-Authenticate` on a 401 has the same MUST-level obligation, so the
    handler propagates the mapping rather than special-casing one header.
    """
    response = client.get("/probe/http-exception-headers", params={"status": 405})

    assert response.headers["allow"] == "GET, HEAD"
    assert response.headers["x-probe"] == "kept"
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.METHOD_NOT_ALLOWED


def test_unrouted_path_returns_not_found_envelope(client: TestClient) -> None:
    """
    A path with no route at all comes back as a NOT_FOUND envelope.

    Same reasoning as the 405: this 404 is produced by the router, not by us, and
    is the most common error any client will ever see.
    """
    response = client.get("/no-such-path")

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND

    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}


# --------------------------------------------------------------------------
# 4. Unhandled exceptions — the leak check
# --------------------------------------------------------------------------


def test_unhandled_exception_returns_internal_error(quiet_client: TestClient) -> None:
    """An exception nobody caught becomes a 500 INTERNAL_ERROR envelope."""
    response = quiet_client.get("/probe/boom")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.INTERNAL_ERROR
    assert detail.message == INTERNAL_ERROR_MESSAGE


def test_unhandled_exception_does_not_leak_exception_text(quiet_client: TestClient) -> None:
    """
    The original exception text appears nowhere in the response.

    ``message`` is documented as safe to show a rider directly. A database error
    carries the host and user in its text; a driver error can carry a full
    connection string. Asserting on the raw body (not the parsed model) is
    deliberate — a leak into an unexpected extra field would still be a leak.
    """
    response = quiet_client.get("/probe/boom")

    assert LEAK_CANARY not in response.text
    assert "connection to host" not in response.text
    assert "postgres" not in response.text
    assert "Traceback" not in response.text
    assert_envelope(response)


def test_unhandled_exception_is_logged_server_side(
    quiet_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Hiding the exception from the rider only works if it is kept somewhere.

    Without this, "don't leak internals" and "silently swallow the bug" look
    identical from the outside.
    """
    with caplog.at_level("ERROR", logger="app.core.errors"):
        quiet_client.get("/probe/boom")

    assert any(
        LEAK_CANARY in record.getMessage() + str(record.exc_info) for record in caplog.records
    )


# --------------------------------------------------------------------------
# 5. The real app is actually wired
# --------------------------------------------------------------------------


def test_real_app_unknown_api_path_is_an_envelope() -> None:
    """
    The dist-less baseline: an unknown /api path is a NOT_FOUND envelope.

    Paired with the SPA tests below, which assert the same thing *with* a built
    frontend present. The two together are the actual guarantee; either alone
    was true at some point while the other was not.
    """
    import app.main

    client = TestClient(app.main.app)

    for method in ("GET", "POST", "DELETE"):
        response = client.request(method, "/api/unknown-path")

        assert response.status_code == HTTPStatus.NOT_FOUND, method
        assert assert_envelope(response).code is ErrorCode.NOT_FOUND, method


# --------------------------------------------------------------------------
# 5a. The SPA catch-all must not swallow unknown /api paths
# --------------------------------------------------------------------------
# `main.py` only registers the SPA fallback `@app.get("/{full_path:path}")` when
# the built frontend exists. It does not exist in a dev checkout, so every test
# above runs against an app where that route was never registered — which is
# precisely how the bug survived: in the deployed container, where `dist` *is*
# present, a GET for an unknown /api path returned `200 text/html` and a POST to
# one returned a method-mismatch 405.
#
# The fixture therefore builds the app the way production builds it: a real
# directory on disk, and a reload of `app.main` so the conditional registration
# actually runs. The directory lives under pytest's `tmp_path`, never inside the
# repository — a crashed test cannot leave a `frontend/dist` artifact behind,
# which a fixture writing to the real path could.


@pytest.fixture
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app, rebuilt with a built frontend present. Restored on teardown."""
    import app.main

    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("// built bundle\n", encoding="utf-8")
    (dist / "index.html").write_text(
        "<!doctype html><html><body>SPA shell</body></html>", encoding="utf-8"
    )

    monkeypatch.setenv("STATIC_FILES_DIR", str(dist))
    get_settings.cache_clear()
    spa_module = importlib.reload(app.main)

    assert spa_module.static_dir.exists(), "fixture did not actually register the SPA fallback"
    assert any(
        getattr(route, "path", "") == "/{full_path:path}" for route in spa_module.app.routes
    ), "the SPA catch-all is not registered — the bug under test would be unreachable"

    try:
        yield TestClient(spa_module.app)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        importlib.reload(app.main)


def test_spa_fallback_still_serves_deep_links(spa_client: TestClient) -> None:
    """
    Sanity check on the fixture and on the fallback's actual purpose.

    If this failed, the tests below could pass for the wrong reason — an app
    that 404s everything satisfies "unknown /api paths are envelopes" while
    breaking a hard refresh of every client-side route.
    """
    response = spa_client.get("/trips/some-slug/map")

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/html")


def test_spa_unknown_api_get_returns_not_found_envelope(spa_client: TestClient) -> None:
    """
    The serious one: a GET for an unknown /api path must not become index.html.

    The offline queue reads `code` out of a JSON body to decide whether a
    request can ever succeed. `200 text/html` is not a failure it can parse at
    all — it is a *success* carrying an HTML document, so the queue would treat
    a request to a path that does not exist as having been accepted.
    """
    response = spa_client.get("/api/unknown-path")

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_spa_unknown_api_non_get_returns_not_found_envelope(
    spa_client: TestClient, method: str
) -> None:
    """
    ...and a non-GET to the same path is a 404, not a 405.

    This is the half a guard *inside* the SPA handler cannot fix: that route is
    GET-only, so it still matches the path for any other verb and the router
    reports a method mismatch. "This path exists but not for POST" is a
    different — and wrong — answer to give the queue than "no such path".
    """
    response = spa_client.request(method, "/api/unknown-path")

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND


def test_spa_unknown_api_path_under_a_real_prefix_is_not_found(spa_client: TestClient) -> None:
    """A path shaped like a contract endpoint, but claimed by no route, is still a 404."""
    response = spa_client.delete("/api/trips/some-slug/stops")

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND


def test_spa_wrong_method_on_a_real_api_route_is_still_405(spa_client: TestClient) -> None:
    """
    The /api catch-all must not swallow a genuine method mismatch.

    It matches every verb, and in Starlette a full match beats the partial match
    that produces a 405 — so the naive version of this fix would turn every
    wrong-verb request on a real endpoint into a 404. Decision-log entry 6 is
    explicit that collapsing 405 into 404 is a contract lie the offline queue
    acts on, so the 405 (with `Allow`) has to be reconstructed deliberately.
    """
    response = spa_client.post("/api/health")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.METHOD_NOT_ALLOWED
    assert "GET" in response.headers.get("allow", "")


def test_unknown_api_catch_all_is_absent_from_the_openapi_spec(spa_client: TestClient) -> None:
    """
    The catch-all is framework plumbing and must not reach Kubb.

    The contract keeps 404-for-no-such-path, 405 and 500 out of the per-endpoint
    tables on purpose; a `/api/{rest:path}` entry in the spec would generate a
    client hook for an endpoint that does not exist.

    Both spellings are asserted: FastAPI renders the route's path into the spec
    with the `:path` convertor stripped, so checking only the source spelling
    would pass against a spec that does contain the catch-all.
    """
    import app.main

    paths = spa_client.app.openapi()["paths"]  # type: ignore[attr-defined]
    declared = app.main.UNKNOWN_API_PATH

    assert declared not in paths
    assert declared.replace(":path", "") not in paths
    assert not any(path.startswith("/api/{rest") for path in paths)


def test_real_app_registers_every_handler() -> None:
    """
    The production app has the same layer attached.

    The probe app proves the handlers behave; this proves ``main.py`` calls
    ``register_exception_handlers``. Neither check substitutes for the other — a
    correct handler that was never registered, and a registered handler that
    returns the wrong thing, are both live bugs.
    """
    import app.main

    handlers = app.main.app.exception_handlers

    assert handlers[ApiError] is api_error_handler
    assert handlers[RequestValidationError] is validation_error_handler
    assert handlers[StarletteHTTPException] is http_exception_handler
    assert handlers[Exception] is unhandled_exception_handler


# --------------------------------------------------------------------------
# 5b. A 405's `Allow` must name the verbs that path really accepts
# --------------------------------------------------------------------------
# The /api catch-all matches every verb, so the router's own 405 never fires
# under /api: the catch-all full-matches first and every wrong-verb request
# lands on `unknown_api_path`, which reconstructs the 405 the router would have
# produced. Two things make that reconstruction easy to get wrong in a way a
# green suite does not notice.
#
# First, *which* routes it can see. FastAPI represents an included router as one
# lazy entry with `path=None` and `methods=None`, so a scan keyed on
# `route.methods` finds only `/api/health` — the single route registered
# directly on `app` — and silently misses all eight contract endpoints. The
# suite asserted on `/api/health` alone and stayed green for the whole time
# `POST /api/trips/{slug}` was answering 404 (decision-log entry 7b).
#
# Second, *what it puts in `Allow`*. Every route in the real app today is
# GET-only, so `Allow: GET` is indistinguishable from a hardcoded string. The
# probe app below is therefore built in production shape — routes behind a real
# `APIRouter`, one path with two verbs and one path with a verb no production
# route uses — and it registers the **production** catch-all handler imported
# from `app.main`. A copy of that handler here would be a second implementation
# free to drift away from the one that ships, which is the failure this whole
# section exists to catch.


def build_router_probe_app() -> FastAPI:
    """The production 405 machinery, wired to routes with more than one verb."""
    import app.main

    probe = FastAPI()
    register_exception_handlers(probe)

    router = APIRouter(prefix="/api")

    @router.get("/both")
    async def probe_both_get() -> dict[str, str]:
        return {"verb": "GET"}

    @router.post("/both")
    async def probe_both_post() -> dict[str, str]:
        return {"verb": "POST"}

    @router.patch("/patch-only")
    async def probe_patch_only() -> dict[str, str]:
        return {"verb": "PATCH"}

    probe.include_router(router)

    # The shipped route, not a re-implementation of it.
    probe.api_route(
        app.main.UNKNOWN_API_PATH,
        methods=app.main._ALL_METHODS,
        include_in_schema=False,
    )(app.main.unknown_api_path)

    return probe


@pytest.fixture
def router_probe_client() -> TestClient:
    """
    The probe app above, rebuilt per test.

    Function-scoped deliberately, though nothing here mutates it. `spa_client`
    reloads `app.main`, so a module-scoped instance would pin whichever
    `unknown_api_path` object existed when the first test in this section ran —
    behaviour cannot drift (the pinned function's `__globals__` still resolve
    live), but `pinned is app.main.unknown_api_path` then reads `False` after a
    reload, and that is the exact identity check used to confirm this probe wires
    the production handler rather than a copy. Rebuilding costs microseconds and
    removes the ordering dependency.
    """
    return TestClient(build_router_probe_app())


def allow_verbs(response: Any) -> set[str]:
    """`Allow` as a set — the contract is the verb set, not its order or spacing."""
    header = response.headers.get("allow", "")
    return {verb.strip() for verb in header.split(",") if verb.strip()}


def test_probe_router_routes_are_reachable(router_probe_client: TestClient) -> None:
    """
    Sanity check on the probe: the catch-all must not shadow the real routes.

    Without this, every assertion below could pass against an app whose router
    never matched anything — "405 for POST /api/patch-only" is also what a
    completely broken app returns.
    """
    assert router_probe_client.get("/api/both").json() == {"verb": "GET"}
    assert router_probe_client.post("/api/both").json() == {"verb": "POST"}
    assert router_probe_client.patch("/api/patch-only").json() == {"verb": "PATCH"}


def test_allow_names_the_verb_that_path_accepts_and_not_get(
    router_probe_client: TestClient,
) -> None:
    """
    The one a hardcoded `Allow: GET` cannot survive.

    A PATCH-only path behind an included router: the verb set has to come from
    the routing table, so PATCH is in it and the two verbs the path does *not*
    accept are not. `Allow` is a MUST on a 405 (RFC 9110 §15.5.6), and a wrong
    one is worse than a missing one — it tells the client to retry a verb that
    will never be accepted.
    """
    response = router_probe_client.post("/api/patch-only")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"PATCH"}


def test_allow_names_every_verb_that_path_accepts(router_probe_client: TestClient) -> None:
    """
    ...and when a path is served by two routes, `Allow` carries both.

    One path, two separate registrations. Reporting only the first match found
    would satisfy the test above and still hand a client an `Allow` that omits a
    verb it could have used.
    """
    response = router_probe_client.delete("/api/both")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET", "POST"}


def test_unknown_probe_path_is_still_a_404_envelope(router_probe_client: TestClient) -> None:
    """A path no probe route claims stays a 404 — `Allow` is derived, not assumed."""
    response = router_probe_client.post("/api/nothing-here")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert assert_envelope(response).code is ErrorCode.NOT_FOUND
    assert "allow" not in response.headers


def test_real_app_wrong_method_on_a_router_registered_route_is_405() -> None:
    """
    The live defect, on the real app: `POST /api/trips/{slug}` was a 404.

    `/api/trips/{slug}` is registered through `api_router`, which is the entire
    difference between this test and the `/api/health` one — and the difference
    the previous implementation could not see.
    """
    import app.main

    response = TestClient(app.main.app).post("/api/trips/whatever")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET"}


def test_probing_leaves_the_live_request_scope_unchanged() -> None:
    """
    The verb probe must leave the in-flight request exactly as it found it.

    A post-condition, not an interleaving check: a write that is restored before
    the call returns is invisible here, deliberately so. Nothing between the
    probe's write and its restore can observe the intermediate state — the loop
    is synchronous, with no `await` in it — so "identical afterwards" is the
    whole guarantee that matters.

    Probing means asking each route "would you match this scope under PATCH?",
    which needs a scope carrying that verb — and `matches()` writes to the scope
    it is handed as well (`_IncludedRouter.matches` inserts FastAPI's bookkeeping
    dict and does not remove it). Done to the live scope, both writes outlive the
    probe: uvicorn reads `scope["method"]` back at *response-send* time, for the
    access-log line (`h11_impl.py`) and to decide whether a HEAD response may
    carry a body (`httptools_impl.py`). A request left holding the last verb
    probed would be logged as a method the client never sent, and would lose its
    body outright the day `_ALL_METHODS` is reordered to end on HEAD.

    Asserted against a copy of the whole scope, not just `method`: the key
    `matches()` inserts is exactly the kind of leftover a `method`-only check
    would miss.

    `scope["fastapi"]` is seeded rather than left absent, because at runtime it
    is never absent: Starlette's router has already iterated `route.matches()`
    over this scope before the catch-all handler runs, and
    `_IncludedRouter.matches` does `scope.setdefault("fastapi", {})`. An empty
    scope exercises only the key-*missing* path. The sentinel inside it is what
    the probe's shallow `{**scope, ...}` copy shares with the live scope, so it
    is the one value a failure to restore would disturb — `before` therefore
    snapshots that inner dict separately, since a plain `dict(scope)` would alias
    it and compare it against itself.
    """
    import app.main

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/trips/whatever",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "app": app.main.app,
        "router": app.main.app.router,
        "fastapi": {"sentinel": object()},
    }
    before = dict(scope) | {"fastapi": dict(scope["fastapi"])}

    assert app.main._methods_allowed_elsewhere(Request(scope)) == {"GET"}
    assert scope == before


def test_spa_wrong_method_on_a_router_registered_route_is_405(spa_client: TestClient) -> None:
    """The same request with a built frontend present — the shape QA found it in."""
    response = spa_client.post("/api/trips/whatever")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET"}


def test_spa_health_get_still_succeeds(spa_client: TestClient) -> None:
    """
    Regression: reconstructing 405s must not cost the routes that already worked.

    The catch-all sits in front of every /api path, so a mistake in the verb
    probe shows up here as a working endpoint answering 404 or 405.
    """
    response = spa_client.get("/api/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("method", ALL_API_METHODS)
def test_spa_unknown_api_path_is_404_under_every_verb(spa_client: TestClient, method: str) -> None:
    """
    A path no route claims is a 404 for *every* verb the catch-all accepts.

    Parametrised over `_ALL_METHODS` itself rather than a hand-picked few: the
    catch-all's whole purpose is to answer regardless of verb, so a verb missing
    from this list is a verb nothing covers. `Allow` must be absent — a 405 here
    would claim the path exists — and the body must never be the SPA shell,
    which is a `200 text/html` a Kubb client would parse as an envelope.

    HEAD is the one named exception: it carries the headers of the GET response
    and no body at all (RFC 9110 §9.3.2), so there is nothing to parse. Every
    other verb is required to carry the envelope, asserted unconditionally —
    keying the check on "did a body arrive" instead would let a regression that
    returns an *empty* 404 for, say, TRACE pass as though it were HEAD.
    """
    response = spa_client.request(method, "/api/no-such-path")

    assert response.status_code == HTTPStatus.NOT_FOUND, method
    assert "allow" not in response.headers, method
    assert "SPA shell" not in response.text, method
    if method == "HEAD":
        assert not response.content, "HEAD must not carry a body"
    else:
        assert response.content, f"{method} must answer with an envelope body, not an empty one"
        assert assert_envelope(response).code is ErrorCode.NOT_FOUND, method
