"""
Every non-2xx response the OpenAPI document declares is an `ErrorEnvelope`.

Kubb generates the frontend client from this document. An operation that
declares no 422 gets FastAPI's auto-injected `HTTPValidationError`, and the
client would type errors as `ErrorEnvelope | HTTPValidationError` — breaking the
one-envelope premise the offline queue relies on (decision-log Entry 17). Nothing
at runtime changes if that happens, so only a document-level guard can see it.

The whole-document checks iterate every operation rather than a fixed list, so
they hold whether or not the SPA fallback is registered (it only exists when
`frontend/dist` exists) and cover any endpoint added later.
"""

from typing import Any

import pytest

ERROR_ENVELOPE_REF = "#/components/schemas/ErrorEnvelope"

# Criterion 3: the declared non-2xx statuses per operation. 405/500 are
# framework-level and deliberately undeclared (api-contract.md, error envelope).
EXPECTED_ERROR_STATUSES = {
    ("get", "/api/trips/{slug}"): {"404", "422"},
    ("get", "/api/trips/{slug}/stops"): {"404", "422"},
    ("get", "/api/trips/{slug}/stops/{stop_id}/photos"): {"404", "422"},
    ("get", "/api/trips/{slug}/map"): {"404", "422"},
    # 401 on the writes since decision-log Entry 29 (t-am-write-gate-legacy): the
    # write gate needs a session. 429 joins these with t-am-rate-limits.
    ("post", "/api/trips/{slug}/stops"): {"401", "403", "404", "409", "422"},
    ("post", "/api/trips/{slug}/stops/{stop_id}/photos"): {"401", "403", "404", "409", "422"},
    ("post", "/api/trips/{slug}/bikes"): {"401", "403", "404", "409", "422"},
    ("patch", "/api/trips/{slug}/bikes/{id}"): {"401", "403", "404", "422"},
}
CREATE_OPERATIONS = [op for op, codes in EXPECTED_ERROR_STATUSES.items() if "409" in codes]
READ_OPERATIONS = [op for op in EXPECTED_ERROR_STATUSES if op[0] == "get"]


@pytest.fixture(scope="module")
def spec() -> Any:
    """The document built fresh — the memoised copy may predate this module."""
    import app.main

    application = app.main.app
    cached = application.openapi_schema
    try:
        application.openapi_schema = None
        return application.openapi()
    finally:
        application.openapi_schema = cached


def operations(spec: dict) -> list[tuple[str, str, dict]]:
    return [(method, path, op) for path, ops in spec["paths"].items() for method, op in ops.items()]


def test_fastapi_validation_schemas_absent(spec: dict) -> None:
    schemas = spec["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_every_declared_error_response_refs_error_envelope(spec: dict) -> None:
    bad = []
    checked = 0
    for method, path, op in operations(spec):
        for code, response in op["responses"].items():
            if code.startswith("2"):
                continue
            checked += 1
            ref = (
                response.get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref")
            )
            if ref != ERROR_ENVELOPE_REF:
                bad.append((method, path, code, ref))
    assert not bad, bad
    # Guards against the loop passing vacuously on an empty/renamed document.
    assert checked >= sum(len(c) for c in EXPECTED_ERROR_STATUSES.values())


def test_no_operation_declares_405_or_500(spec: dict) -> None:
    declared = [
        (method, path, code)
        for method, path, op in operations(spec)
        for code in op["responses"]
        if code in {"405", "500"}
    ]
    assert not declared


@pytest.mark.parametrize(("method", "path"), list(EXPECTED_ERROR_STATUSES))
def test_declared_error_statuses_per_operation(spec: dict, method: str, path: str) -> None:
    responses = spec["paths"][path][method]["responses"]
    errors = {code for code in responses if not code.startswith("2")}
    assert errors == EXPECTED_ERROR_STATUSES[(method, path)]


@pytest.mark.parametrize(("method", "path"), CREATE_OPERATIONS)
def test_create_operations_keep_200_replay(spec: dict, method: str, path: str) -> None:
    responses = spec["paths"][path][method]["responses"]
    assert {"200", "201"} <= set(responses)
    assert "replay" in responses["200"]["description"].lower()


@pytest.mark.parametrize(("method", "path"), READ_OPERATIONS)
def test_read_422_describes_path_parameters(spec: dict, method: str, path: str) -> None:
    description = spec["paths"][path][method]["responses"]["422"]["description"]
    assert "path parameter" in description.lower()
