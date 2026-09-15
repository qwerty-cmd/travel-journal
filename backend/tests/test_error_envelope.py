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
2. **No database, no Docker** — with one deliberate exception, in section 4a.
   Everything else here is in-process, and if *that* changes, the exception
   layer has grown a dependency it should not have. Section 4a needs a genuine
   SQLAlchemy ``StatementError`` carrying a bound parameter, which only a real
   driver round trip produces; the reasoning is written out there.
"""

from __future__ import annotations

import importlib
import logging
import traceback
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError, StatementError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.errors import (
    INTERNAL_ERROR_MESSAGE,
    ApiError,
    api_error_handler,
    code_for_status,
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

# A 5xx `HTTPException` detail: diagnostic, and deliberately carrying no slug of
# its own. Section 4a's http_exception case has to be able to blame the URL and
# nothing else for a slug appearing in the log line.
HTTP_DETAIL_TEXT = "upstream tile service returned 503 after 30s"

# A stand-in trip slug, for section 4a. Shaped like a real one (URL-safe token,
# no characters the client would percent-encode) so it travels through the path
# exactly as an issued slug does, and unique enough that finding it in a log
# record can only mean it came from this request.
SLUG_CANARY = "SLUG-CANARY-Kt7xQ2wvB9-do-not-log"

# The probe path whose template differs from the URL a client sends — which is
# the whole of what section 4a asserts. `{slug}` is the template; the concrete
# path carries `SLUG_CANARY` in its place.
SLUG_PATH = "/trips/{slug}"


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

    # Section 4a. One path with a `{slug}` template, three verbs, one per 5xx
    # logging site — so each handler is exercised against a request whose URL
    # contains a credential and whose route template does not. Same path on
    # purpose: the log line has to name the method as well, and three identical
    # templates make a handler that logged the wrong one obvious.

    @app.get(SLUG_PATH)
    async def probe_slug_api_error(slug: str) -> None:
        raise ApiError.internal(DB_ERROR_TEXT)

    @app.post(SLUG_PATH)
    async def probe_slug_http_exception(slug: str) -> None:
        raise StarletteHTTPException(status_code=500, detail=HTTP_DETAIL_TEXT)

    @app.delete(SLUG_PATH)
    async def probe_slug_boom(slug: str) -> None:
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
        (ApiError.conflict, HTTPStatus.CONFLICT, ErrorCode.CONFLICT),
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
        # ...and the same two directions for the sixth code (decision-log entry
        # 14). A create handler is the one place a 404 and a 409 sit next to each
        # other — "no such trip" and "that id belongs to another trip" are both
        # reachable from the same request — so this is the transposition most
        # likely to be typed.
        (409, ErrorCode.NOT_FOUND),
        (404, ErrorCode.CONFLICT),
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
        (409, ErrorCode.CONFLICT),
        (500, ErrorCode.INTERNAL_ERROR),
        # An unnamed status -> INTERNAL_ERROR, per the mapping. `418` and not
        # `409`: this case needs a status the contract will *keep* saying nothing
        # about, and `409`/`CONFLICT` is entering `_STATUS_TO_CODE` (decision-log
        # entry 14), which would turn this row red for a reason that has nothing
        # to do with what it asserts. `418` is a registered HTTP status no
        # endpoint in this contract can ever return.
        (418, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_direct_construction_still_allows_every_contract_pair(status: int, code: ErrorCode) -> None:
    """
    The backstop rejects contradictions, not legitimate use.

    A guard that refused a pair the contract allows would be worse than none: it
    would push call sites back onto raising bare `HTTPException`s.
    """
    error = ApiError(status_code=status, code=code, message="fine")

    assert error.status_code == status
    assert error.code is code


# --------------------------------------------------------------------------
# 2c. CONFLICT — the sixth code
# --------------------------------------------------------------------------
# Decision-log entry 14: a create request whose client-generated id already
# exists under a *different* parent is neither malformed nor a server fault, so
# it had no legal code among the first five. `409` previously fell through to
# `INTERNAL_ERROR` — which the offline queue reads as "the server broke, retry
# later", and so would have retried forever a request that can never succeed.
# The mapping row is what makes the code reachable, and the never-retry
# classification is what makes it correct.
#
# Nothing raises a 409 yet — `t-stops-create-endpoint` is the first raise site,
# and the message leak boundary (no value from the conflicting record) is tested
# there, at the raise site, because it is enforced there. What is testable today
# is the envelope machinery: the constructor, the mapping, and the backstop.


def test_conflict_classmethod_status() -> None:
    """`ApiError.conflict` is a 409..."""
    error = ApiError.conflict("That id already belongs to another trip.")

    assert error.status_code == HTTPStatus.CONFLICT


def test_conflict_classmethod_code() -> None:
    """...carrying CONFLICT, asserted apart from the status (see section 2's note)."""
    error = ApiError.conflict("That id already belongs to another trip.")

    assert error.code is ErrorCode.CONFLICT


def test_conflict_message_is_verbatim_on_the_wire(client: TestClient) -> None:
    """
    A CONFLICT message reaches the rider exactly as the raise site wrote it.

    CONFLICT is not INTERNAL_ERROR: the handler must not substitute the generic
    string for it. The raise site is the only place that knows how to say "that
    id is taken" without naming the trip that took it, so the handler passing the
    string through unchanged is what that boundary depends on.
    """
    message = "That stop id already exists on a different trip."
    response = client.get(
        "/probe/api-error",
        params={"status": 409, "code": "CONFLICT", "message": message},
    )

    assert response.status_code == HTTPStatus.CONFLICT
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.CONFLICT
    assert detail.message == message
    assert detail.message != INTERNAL_ERROR_MESSAGE


def test_code_for_status_maps_409_to_conflict() -> None:
    """
    409 lands on CONFLICT in the mapping itself, not only through a handler.

    Asserted directly because this is the single row that changed behaviour: with
    it absent, 409 falls through to INTERNAL_ERROR, and every other assertion in
    this section is downstream of it.
    """
    assert code_for_status(409) is ErrorCode.CONFLICT
    assert code_for_status(409) is not ErrorCode.INTERNAL_ERROR


def test_conflict_pairing_backstop_raises_value_error_not_assertion_error() -> None:
    """
    The transposed-pair backstop is a `ValueError`, deliberately not an `assert`.

    `assert` is stripped under `python -O`, which would silently disable the
    check in exactly the optimised build where a contradictory body goes
    unnoticed on the wire. `pytest.raises(ValueError)` alone already fails an
    `AssertionError`; the type is pinned exactly here as well so the intent is
    written down and not merely implied by the class chosen.
    """
    with pytest.raises(ValueError) as exc_info:
        ApiError(status_code=409, code=ErrorCode.NOT_FOUND, message="nope")

    assert type(exc_info.value) is ValueError
    assert not isinstance(exc_info.value, AssertionError)


def test_error_code_is_exactly_the_contract_list() -> None:
    """
    The enum matches the contract's table — six members, these names, these values.

    A ratchet, in the shape of the convention ratchets in
    ``test_trip_metadata_endpoint.py`` and ``test_stops_list_endpoint.py``: the
    value is not in catching today's members, it is in failing at the moment a
    seventh is added, next to the prose that has to be updated with it. Adding a
    code is a contract change (decision-log entries 6 and 14) — twice now it has
    been escalated rather than invented, and twice the count in the surrounding
    prose went stale because nothing forced the author back to it.

    Values as well as names: the value is the JSON string the offline queue
    branches on, so renaming ``CONFLICT`` to ``"conflict"`` would be a silent
    wire-format change that a names-only check would wave through.
    """
    assert {member.name: member.value for member in ErrorCode} == {
        "FORBIDDEN": "FORBIDDEN",
        "NOT_FOUND": "NOT_FOUND",
        "VALIDATION_ERROR": "VALIDATION_ERROR",
        "METHOD_NOT_ALLOWED": "METHOD_NOT_ALLOWED",
        "CONFLICT": "CONFLICT",
        "INTERNAL_ERROR": "INTERNAL_ERROR",
    }


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
        (409, ErrorCode.CONFLICT),
        (418, ErrorCode.INTERNAL_ERROR),  # unnamed status -> INTERNAL_ERROR; see above for 418
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
# 4a. A trip slug is the credential — it must not reach a 5xx log line
# --------------------------------------------------------------------------
# There are no accounts here: a trip slug *is* the authorization for a link
# (spec Section 4, decision-log Entry 13). A 5xx on `GET /api/trips/{slug}` had
# three places to write one down, and this section binds the two that were
# closed:
#
#   A. The traceback. SQLAlchemy renders `[parameters: ('<slug>', ...)]` into
#      `StatementError.__str__`, and `logger.exception(exc_info=...)` emits it.
#      Closed by `hide_parameters=True` on the engine in `app/data/db.py`.
#   B. The handlers' own format arguments — `request.url.path` *is*
#      `/api/trips/<slug>`. Closed by `_endpoint()` in `app/core/errors.py`,
#      which logs the matched route template. All three 5xx sites use it, so all
#      three are asserted below: enumerating the places a rule is enforced is
#      Entry 7(a)'s rule, and mutating one of them proves nothing about the
#      other two.
#
# Sink C, the uvicorn access log, is deliberately accepted and out of scope
# (`app/core/errors.py` says why, and names the task that reopens it). Nothing
# here asserts against `uvicorn.access` — a test there would bind a behaviour
# this project chose not to guarantee.
#
# **Every negative assertion below is paired with a positive on the same
# record.** "The canary is not in the log" passes for at least four reasons that
# have nothing to do with redaction: the handler logged nothing, the probe never
# fired, `caplog` was pointed at the wrong logger, or `caplog` captured nothing
# at all. `sole_error_record` closes all four — it fails unless exactly one
# ERROR record from `app.core.errors` was captured — and each test then asserts
# what the record *does* say (the route template, the handler that wrote it, the
# diagnostic detail) before asserting what it does not.


def sole_error_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """
    The one ERROR record `app.core.errors` emitted, or a failure saying so.

    The anti-vacuity guard for every assertion in this section. Asserting
    *exactly* one rather than "at least one" is deliberate: a handler that
    logged the endpoint safely and then logged the raw URL again on a second
    line would satisfy "some record has no slug in it".
    """
    records = [
        record
        for record in caplog.records
        if record.name == "app.core.errors" and record.levelno >= logging.ERROR
    ]
    assert len(records) == 1, (
        f"expected exactly one ERROR record from app.core.errors, got {len(records)}: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    return records[0]


def rendered(record: logging.LogRecord) -> str:
    """
    Everything a log handler would write for this record — message *and* traceback.

    `logger.exception` puts the interesting text in `exc_info`, not in the
    format string, so a check against `getMessage()` alone would miss sink A
    entirely and pass while the parameters were still being printed.
    """
    text_parts = [record.getMessage()]
    if record.exc_info:
        text_parts.extend(traceback.format_exception(*record.exc_info))
    return "".join(text_parts)


async def test_a_bound_slug_is_not_rendered_into_the_500_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Sink A, against a **real** `StatementError` with the slug bound into it.

    This is the one test in the module that touches Postgres, and it has to.
    `hide_parameters=True` is a property of the engine `app/data/db.py` builds,
    and the `[parameters: ...]` rendering is SQLAlchemy's — neither is
    observable without a genuine driver error carrying genuine bound parameters.
    A `RuntimeError` probe would be **vacuous by construction**: it has no bound
    parameters, so a redaction assertion against it cannot fail (decision-log
    Entry 7's `/api/health` failure mode in a new costume).

    The statement names a table that does not exist, so the failure is a plain
    `42P01` from the driver: it needs no migrations, reads nothing and writes
    nothing, and leaves no row behind.

    The engine is the **production** one, disposed again in this test's own
    event loop — `app/data/db.py`'s engine is the object under test, so an
    engine built here would only assert that this test passed the flag it just
    chose. Disposing in `finally` is what keeps its warning satisfied: no
    connection created on this loop survives into the next test's.

    Criterion 2 — diagnosability — is asserted first and is the larger half of
    this test. A 500 that cannot be diagnosed is not an acceptable price for
    redaction: the exception class, the driver's own message, the `[SQL: ...]`
    statement and the traceback must all still be there.
    """
    from app.data import db

    raised: list[StatementError] = []
    missing_table = "no_such_table_slug_redaction_probe"

    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get(SLUG_PATH)
    async def probe_slug_db_failure(slug: str) -> None:
        async with db.engine.connect() as conn:
            try:
                await conn.execute(
                    text(f"SELECT 1 FROM {missing_table} WHERE rider_slug = :slug"),
                    {"slug": slug},
                )
            except SQLAlchemyError as exc:
                # Kept so the test can prove the slug really was bound into the
                # failing statement. Without it, "no slug in the log" would also
                # be true of an error that never carried one.
                assert isinstance(exc, StatementError)
                raised.append(exc)
                raise

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    try:
        with caplog.at_level("ERROR", logger="app.core.errors"):
            async with AsyncClient(transport=transport, base_url="http://probe") as probe_client:
                response = await probe_client.get(SLUG_PATH.format(slug=SLUG_CANARY))
    finally:
        await db.engine.dispose()

    # The probe fired, and the rider got the contract's answer.
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert assert_envelope(response).code is ErrorCode.INTERNAL_ERROR

    # The error genuinely carried the slug as a bound parameter. This is the
    # assertion that makes the redaction check below non-vacuous.
    assert len(raised) == 1
    assert SLUG_CANARY in str(raised[0].params)

    record = sole_error_record(caplog)
    log_text = rendered(record)

    # Criterion 2: still diagnosable. Class, driver message, SQL, traceback.
    assert type(raised[0]).__name__ in log_text
    assert missing_table in log_text
    assert "[SQL:" in log_text
    assert "Traceback (most recent call last)" in log_text
    assert "Unhandled exception on" in log_text

    # Criterion 1: and the credential is not in it.
    assert "[parameters:" not in log_text
    assert SLUG_CANARY not in log_text


def test_api_error_500_log_names_the_route_not_the_slug(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Sink B on `api_error_handler` — the INTERNAL_ERROR branch.

    Same arrangement as `test_api_error_internal_is_logged_server_side`, which
    asserts the positive half (the withheld message is kept): the two halves
    belong on one record, because "the diagnostic survived" and "the credential
    did not" are the two things that can only both be true if the handler
    formats an endpoint rather than a URL.
    """
    with caplog.at_level("ERROR", logger="app.core.errors"):
        response = client.get(SLUG_PATH.format(slug=SLUG_CANARY))

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    record = sole_error_record(caplog)
    log_text = rendered(record)

    assert f"GET {SLUG_PATH}" in log_text
    assert "ApiError" in log_text
    assert LEAK_CANARY in log_text  # the withheld diagnostic is still logged

    assert SLUG_CANARY not in log_text


def test_http_exception_500_log_names_the_route_not_the_slug(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Sink B on `http_exception_handler` — the branch that maps a 5xx to INTERNAL_ERROR.

    The verb is `POST` on the same template, so the assertion also fails a
    handler that logged a hardcoded or borrowed method.
    """
    with caplog.at_level("ERROR", logger="app.core.errors"):
        response = client.post(SLUG_PATH.format(slug=SLUG_CANARY))

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    record = sole_error_record(caplog)
    log_text = rendered(record)

    assert f"POST {SLUG_PATH}" in log_text
    assert "HTTPException" in log_text
    assert HTTP_DETAIL_TEXT in log_text  # the withheld detail is still logged

    assert SLUG_CANARY not in log_text


def test_unhandled_exception_log_names_the_route_not_the_slug(
    quiet_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Sink B on `unhandled_exception_handler`, independently of sink A.

    The exception here is a `RuntimeError` — no bound parameters, nothing for
    `hide_parameters` to hide — which is exactly why it belongs *only* to sink
    B: the slug reaches this log line through `request.url.path`, whatever the
    exception type, and no database error is required to put it there.
    """
    with caplog.at_level("ERROR", logger="app.core.errors"):
        response = quiet_client.delete(SLUG_PATH.format(slug=SLUG_CANARY))

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    record = sole_error_record(caplog)
    log_text = rendered(record)

    assert f"DELETE {SLUG_PATH}" in log_text
    assert "RuntimeError" in log_text
    assert LEAK_CANARY in log_text  # the exception itself is still kept

    assert SLUG_CANARY not in log_text


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
    """
    A path shaped like a contract endpoint, but claimed by no route, is still a 404.

    **The path moved on 2026-09-15; the subject did not.** This case used to send
    `DELETE /api/trips/some-slug/stops`, which was unclaimed only while the stops
    router was an empty stub. `t-stops-list-endpoint` registered `GET`+`HEAD`
    there, so that request is now a genuine method mismatch and its correct
    answer is `405` — asserted in `test_spa_delete_on_the_registered_stops_path_
    is_405` below, which is where the old request went. Keeping the old path here
    would have quietly converted this test from "no such path" into "wrong verb",
    testing the same thing as its neighbour and leaving the 404-under-a-real-
    prefix case uncovered.

    The replacement is chosen so it cannot expire the same way: the contract
    lists **eight** endpoints and says "No others", and `/api/trips/{slug}/
    itinerary` is not one of them, so no future task registers it. A path from
    the eight (`/bikes`, `.../photos`) would only have reset this same clock.
    """
    response = spa_client.delete("/api/trips/some-slug/itinerary")

    assert response.status_code == HTTPStatus.NOT_FOUND
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.NOT_FOUND
    assert "allow" not in response.headers, "a 405 here would claim the path exists"


def test_spa_delete_on_the_registered_stops_path_is_405(spa_client: TestClient) -> None:
    """
    `DELETE /api/trips/{slug}/stops` — the expiry the contract predicted, now fired.

    `docs/api-contract.md`'s forward-note and decision-log entry 6's footnote both
    named this exact moment: while the stops router had no methods registered,
    Starlette found no route at all and `404` was the right answer; with
    `GET`+`HEAD` registered by `t-stops-list-endpoint` the path exists, `DELETE`
    mismatches, and `405` / `METHOD_NOT_ALLOWED` is what the contract requires.

    It is also the positive evidence for `t-405-router-route-collapse`: those
    notes warned that without it this would *silently stay* `404` — the /api
    catch-all full-matches every verb, so the router's own 405 never fires under
    /api and the status has to be reconstructed. It was not silent.

    `Allow` is asserted as an exact set for the reason its `/api/health` sibling
    gives: a substring check survives a hardcoded string, a set that lost `HEAD`
    and a set that grew a verb the path does not accept. `{"GET", "HEAD"}` is the
    stops path's real verb set today — `HEAD` from the second, schema-excluded
    registration of the same handler. When `POST /trips/{slug}/stops` lands this
    set becomes `{"GET", "HEAD", "POST"}` and this assertion is *supposed* to
    fail; that is the check doing its job, not a stale test.
    """
    response = spa_client.delete("/api/trips/some-slug/stops")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET", "HEAD"}


def test_spa_wrong_method_on_a_real_api_route_is_still_405(spa_client: TestClient) -> None:
    """
    The /api catch-all must not swallow a genuine method mismatch.

    It matches every verb, and in Starlette a full match beats the partial match
    that produces a 405 — so the naive version of this fix would turn every
    wrong-verb request on a real endpoint into a 404. Decision-log entry 6 is
    explicit that collapsing 405 into 404 is a contract lie the offline queue
    acts on, so the 405 (with `Allow`) has to be reconstructed deliberately.

    `Allow` is asserted as an exact verb set, not as "GET appears somewhere in
    the header". A substring check passes against `Allow: GET` and equally
    against a hardcoded one, against a set that lost HEAD, and against one that
    grew a verb this path does not accept — and it survived the GET -> HEAD
    change without anyone editing it, which is how you find out a check is not
    watching. `HEAD` is in the set because `/api/health` carries a second,
    schema-excluded HEAD registration of the same handler.
    """
    response = spa_client.post("/api/health")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    detail = assert_envelope(response)
    assert detail.code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET", "HEAD"}


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
    """
    The production 405 machinery, wired to routes with more than one verb.

    **The probe's GET routes deliberately carry no HEAD sibling**, so "production
    shape" is now approximate: since `t-head-on-get-routes`, every real GET path
    is served by a second, schema-excluded HEAD registration, and `@router.get`
    here is not. The divergence is recorded rather than closed, because copying
    the pattern in would cost an assertion and buy nothing:

    - What this probe exists to prove is that the verb set comes from the routing
      table instead of a hardcoded `Allow: GET` — hence a PATCH-only path and a
      path served by two routes. A HEAD sibling would be a *third* instance of
      "one path, two routes", which `/api/both`'s GET+POST pair already covers,
      while turning the sharp `{"GET", "POST"}` assertion into
      `{"GET", "POST", "HEAD"}` — a set in which the interesting verbs are
      harder to read and a spurious HEAD is impossible to spot.
    - The HEAD shape is not left uncovered by that choice: it is asserted
      against the **real app**, in `test_real_app_wrong_method_on_a_router_
      registered_route_is_405` and its `spa_client` twin, both of which now
      expect `{"GET", "HEAD"}`. Production shape asserted on production is
      better evidence than production shape imitated here.

    So this probe covers the verb-*derivation* logic, and the real-app tests
    cover the registrations. If a future change makes HEAD behave differently
    from any other verb in `_ALL_METHODS`, it is the real-app tests that must
    grow, not this fixture.
    """
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

    `HEAD` joined the expected set with `t-head-on-get-routes`: the path is
    served by two routes now, the documented GET one and a schema-excluded HEAD
    registration of the same handler, and `Allow` names every verb the *path*
    accepts. Its presence here is therefore also a live check that the verb
    probe reads both routes rather than stopping at the first match.
    """
    import app.main

    response = TestClient(app.main.app).post("/api/trips/whatever")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET", "HEAD"}


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

    assert app.main._methods_allowed_elsewhere(Request(scope)) == {"GET", "HEAD"}
    assert scope == before


def test_spa_wrong_method_on_a_router_registered_route_is_405(spa_client: TestClient) -> None:
    """The same request with a built frontend present — the shape QA found it in."""
    response = spa_client.post("/api/trips/whatever")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert assert_envelope(response).code is ErrorCode.METHOD_NOT_ALLOWED
    assert allow_verbs(response) == {"GET", "HEAD"}


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
