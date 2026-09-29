"""
The two `ErrorCode`s decision-log Entry 29 admitted: 401 UNAUTHENTICATED and 429 RATE_LIMITED.

Covers the status map, the two `ApiError` constructors and the headers they owe
(`WWW-Authenticate`, `Retry-After`), the `__init__` pairing backstop for the new
pairs, and the `ERROR_RESPONSES` entries that put them into the OpenAPI document.
Task `t-am-contract-models`. The eight-member ratchet itself lives in
`test_error_envelope.py`.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from conftest import make_test_client
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.responses import ERROR_RESPONSES, error_responses
from app.core.errors import ApiError, code_for_status, register_exception_handlers
from app.models.common import ErrorCode, ErrorEnvelope


def _client_raising(error: ApiError) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe", responses=error_responses({HTTPStatus.TOO_MANY_REQUESTS: "Slow down."}))
    async def probe() -> None:
        raise error

    return make_test_client(app)


def test_error_code_has_exactly_eight_members() -> None:
    assert len(ErrorCode) == 8
    assert ErrorCode.UNAUTHENTICATED.value == "UNAUTHENTICATED"
    assert ErrorCode.RATE_LIMITED.value == "RATE_LIMITED"


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, ErrorCode.UNAUTHENTICATED), (429, ErrorCode.RATE_LIMITED)],
)
def test_code_for_status_maps_the_new_statuses(status: int, code: ErrorCode) -> None:
    assert code_for_status(status) is code


def test_unauthenticated_renders_a_401_envelope_with_www_authenticate() -> None:
    response = _client_raising(ApiError.unauthenticated("Sign in to continue.")).get("/probe")

    assert response.status_code == 401
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code is ErrorCode.UNAUTHENTICATED
    assert envelope.error.message == "Sign in to continue."
    assert response.headers["www-authenticate"] == 'Cookie realm="bike-trip-journal"'


def test_rate_limited_renders_a_429_envelope_with_retry_after() -> None:
    response = _client_raising(ApiError.rate_limited("Too many attempts.", 42)).get("/probe")

    assert response.status_code == 429
    envelope = ErrorEnvelope.model_validate(response.json())
    assert envelope.error.code is ErrorCode.RATE_LIMITED
    assert envelope.error.message == "Too many attempts."
    assert response.headers["retry-after"] == "42"


def test_rate_limited_accepts_one_second() -> None:
    assert ApiError.rate_limited("Wait.", 1).headers == {"Retry-After": "1"}


@pytest.mark.parametrize("seconds", [0, -1])
def test_rate_limited_below_one_second_raises(seconds: int) -> None:
    with pytest.raises(ValueError):
        ApiError.rate_limited("Wait.", seconds)


def test_other_constructors_carry_no_headers() -> None:
    response = _client_raising(ApiError.forbidden("No.")).get("/probe")

    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert "retry-after" not in response.headers


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.RATE_LIMITED),
        (429, ErrorCode.UNAUTHENTICATED),
        (401, ErrorCode.FORBIDDEN),
        (403, ErrorCode.UNAUTHENTICATED),
        (429, ErrorCode.INTERNAL_ERROR),
        (500, ErrorCode.RATE_LIMITED),
    ],
)
def test_pairing_backstop_rejects_mismatched_new_pairs(status: int, code: ErrorCode) -> None:
    with pytest.raises(ValueError):
        ApiError(status, code, "mismatch")


def test_error_responses_declare_401_and_429_as_envelopes() -> None:
    assert ERROR_RESPONSES[HTTPStatus.UNAUTHORIZED]["model"] is ErrorEnvelope
    assert ERROR_RESPONSES[HTTPStatus.TOO_MANY_REQUESTS]["model"] is ErrorEnvelope


def test_429_declares_retry_after_in_openapi() -> None:
    """The header reaches the rendered document, not only the constant."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe", responses=error_responses({HTTPStatus.TOO_MANY_REQUESTS: "Slow down."}))
    async def probe() -> None:
        return None

    response = app.openapi()["paths"]["/probe"]["get"]["responses"]["429"]
    assert response["description"] == "Slow down."
    assert "Retry-After" in response["headers"]
    assert response["headers"]["Retry-After"]["schema"]["type"] == "integer"
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorEnvelope")
