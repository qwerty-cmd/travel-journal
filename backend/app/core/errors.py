"""
Global exception layer — the wiring behind the API contract's error envelope.

`app/models/common.py` defines the *shape* every non-2xx response takes
(`ErrorEnvelope`). This module is what makes that promise true of responses the
application never explicitly builds: FastAPI's own `{"detail": [...]}` 422,
Starlette's routing 404/405, and an unhandled exception's 500. Without it, a
client parsing errors would need one code path per failure origin — and the
offline queue, which has to decide retry-vs-never-retry programmatically from
the response body, is the consumer that can least afford that.

Every body here is produced by dumping the Pydantic model, never by writing a
dict literal. The model is the contract; a literal is a copy of the contract
that drifts.

Raise `ApiError` from application code (`core/security.py` raises FORBIDDEN and
NOT_FOUND through it). The other three handlers exist to normalise failures that
originate in the framework rather than in our code.

**Raise it through the classmethod constructors** — `ApiError.forbidden(...)`,
`ApiError.not_found(...)`, `ApiError.validation(...)`, `ApiError.internal(...)` —
never by passing a status and a code as separate arguments. The contract's
top-priority distinction is 403-vs-404 (spec Section 12, decision-log entry 6),
and a two-argument call site makes the transposed pair `(404, FORBIDDEN)`
representable: a body that violates the contract, produced by a typo, that no
status-only or code-only assertion would catch. The classmethods make it
unrepresentable; `__init__` rejects it as a backstop for anything that still
constructs `ApiError` directly.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.common import ErrorCode, ErrorDetail, ErrorEnvelope

logger = logging.getLogger(__name__)

# The one message a rider is ever shown for a server fault. Deliberately fixed
# and content-free: the original exception text can carry a database host, a
# connection string, a SQL fragment or a file path, and `ErrorDetail.message` is
# documented as safe to display directly. The real exception is logged instead.
INTERNAL_ERROR_MESSAGE = "Something went wrong on our end. Please try again."

# Status -> contract code. Only the statuses the contract's eight endpoints
# actually return get a specific code; everything else (including a 500) is an
# INTERNAL_ERROR, because the contract defines exactly five codes and inventing a
# sixth at runtime would produce a body the generated client cannot type.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    HTTPStatus.FORBIDDEN: ErrorCode.FORBIDDEN,
    HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    HTTPStatus.UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
}


def code_for_status(status_code: int) -> ErrorCode:
    """The `ErrorCode` the contract pairs with an HTTP status."""
    return _STATUS_TO_CODE.get(status_code, ErrorCode.INTERNAL_ERROR)


class ApiError(Exception):
    """
    The single exception application code raises to return an error envelope.

    Carries the HTTP status, the contract `ErrorCode` and the rider-facing
    message together, so the call site decides all three at once instead of a
    handler inferring the code from a status after the fact.

    Prefer the classmethods below to the constructor: they pair the status and
    the code for you, so a call site cannot emit a 404 that reports FORBIDDEN.
    """

    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        expected = code_for_status(status_code)
        if code is not expected:
            # A backstop, not the primary guard — the classmethods are. Raised
            # rather than `assert`ed on purpose: `assert` is stripped under
            # `python -O`, which would silently disable the check in exactly the
            # optimised build where a contradictory body would go unnoticed.
            raise ValueError(
                f"ApiError status {status_code} must carry {expected}, not {code}. "
                f"The contract maps status to code (see code_for_status); a pair that "
                f"disagrees would put a contract-violating body on the wire. Use "
                f"ApiError.forbidden/.not_found/.validation/.internal instead."
            )
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    @classmethod
    def forbidden(cls, message: str) -> ApiError:
        """403 — a valid slug used where it has no write permission. `message` is shown."""
        return cls(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN, message)

    @classmethod
    def not_found(cls, message: str) -> ApiError:
        """404 — no such trip/stop/bike/path. `message` is shown to the rider."""
        return cls(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, message)

    @classmethod
    def validation(cls, message: str) -> ApiError:
        """
        422 — a request that passed schema validation but failed a rule we enforce.

        FastAPI's own schema failures never come through here; they arrive as
        `RequestValidationError`. This is for the checks Pydantic can't express.
        """
        return cls(HTTPStatus.UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR, message)

    @classmethod
    def internal(cls, message: str) -> ApiError:
        """
        500 — a server fault. `message` is **logged, never returned**.

        Pass the real diagnostic text (the driver error, the failing key): the
        handler swaps it for `INTERNAL_ERROR_MESSAGE` on the way out, because the
        contract guarantees an INTERNAL_ERROR body is a fixed generic string.
        """
        return cls(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR, message)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ApiError(status_code={self.status_code}, "
            f"code={self.code!r}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Why 5xx logging never formats a concrete URL (decision-log Entry 13).
#
# A trip slug is not an identifier here — it *is* the credential, the whole of
# the authorization for a link. `GET /api/trips/{slug}` is live, so a 5xx on it
# had three places to write that slug down:
#
#   A. The traceback. SQLAlchemy's `StatementError.__str__` appends
#      `[parameters: ('<slug>', ...)]`, which `logger.exception(exc_info=...)`
#      renders. CLOSED in `app/data/db.py` via `hide_parameters=True`.
#   B. The handlers' own format arguments. `request.url.path` *is*
#      `/api/trips/<slug>` — so the slug landed in the ERROR record whatever the
#      exception type, no database error required. CLOSED here: every 5xx site
#      goes through `_endpoint()` and logs the matched route template instead.
#   C. The uvicorn access log, which writes the request line on every request.
#      ACCEPTED, deliberately, not overlooked: there is no shipper, tracker or
#      aggregator in this project today; silencing access logs wholesale would
#      cost status/method/latency for every request to remove a URL that was
#      already on the wire; and an app module reconfiguring a third-party
#      logger at import time is the silently-inert shape Entry 7(b) records.
#      Filed as `t-access-log-slug-exposure`, trigger: **the first error
#      tracker or log shipper configured**. Fire it then.
#
# REJECTED REASONING — do not delete this guard because "the slug is in the
# access log anyway, so this is pointless." That argument was made, recorded in
# full and lost (Entry 13): it is a claim about *severity*, and the triage gate
# asks about *currency*. C being open is the reason B is worth closing, not a
# reason it isn't — B is the copy that follows an exception into a tracker.
# Reopening this means showing Entry 13's evidence no longer applies.
# ---------------------------------------------------------------------------
def _endpoint(request: Request) -> str:
    """
    `"GET /trips/{slug}"` — which endpoint failed, with no concrete URL in it.

    The matched route's template, never `request.url.path`. One helper so all
    three 5xx call sites share a single guard rather than three that can drift.
    Falls back when nothing matched (a routing 404 — which never reaches a 5xx
    log line, but the helper must not depend on that).
    """
    template = getattr(request.scope.get("route"), "path", None)
    return f"{request.method} {template or '<unmatched>'}"


def envelope_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Serialise an error through `ErrorEnvelope` — the only way a body is built here."""
    envelope = ErrorEnvelope(error=ErrorDetail(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """
    `ApiError` -> its own status and code; its message only if that is safe to show.

    An INTERNAL_ERROR message is replaced by the fixed generic string and logged
    instead, exactly as for the two framework paths. `ApiError` is documented as
    the single exception application code raises, so the moment `data/` or
    `security.py` wraps a driver error in one — `ApiError.internal(str(exc))` —
    this handler is holding a database host and password. The leak boundary has
    to be enforced on every door into the envelope, not only on the ones a
    framework happens to own.
    """
    if exc.code is ErrorCode.INTERNAL_ERROR:
        logger.error(
            "ApiError %s INTERNAL_ERROR on %s: %s",
            exc.status_code,
            _endpoint(request),
            exc.message,
        )
        return envelope_response(exc.status_code, exc.code, INTERNAL_ERROR_MESSAGE)

    return envelope_response(exc.status_code, exc.code, exc.message)


def _format_validation_errors(errors: list[dict[str, Any]]) -> str:
    """
    A short, rider-readable summary of what failed validation.

    Only the field location and pydantic's own message are used — never the
    submitted `input`, which is user data we have no reason to echo back.
    """
    parts: list[str] = []
    for error in errors:
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
        message = str(error.get("msg", "Invalid value"))
        parts.append(f"{location}: {message}" if location else message)
    if not parts:
        return "The request could not be validated."
    return "The request could not be validated. " + "; ".join(parts)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    FastAPI's `{"detail": [...]}` 422 -> the envelope, with VALIDATION_ERROR.

    This is the one the contract calls out by name: without it, a 422 is the
    single response shape the generated client can't parse like the others.
    """
    return envelope_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        ErrorCode.VALIDATION_ERROR,
        _format_validation_errors(exc.errors()),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Any `HTTPException` — ours or Starlette's own routing 404/405 — as an envelope.

    Starlette raises these before any route function runs, so a request for a
    path that doesn't exist is normalised here too, not only failures raised
    inside a handler.

    `exc.headers` is carried onto the response. Replacing FastAPI's default
    handler means we also inherit its obligations: a 405 must carry `Allow`
    (RFC 9110 §15.5.6 makes it a MUST), and a 401 its `WWW-Authenticate`.
    Normalising the *body* is not a licence to drop the headers that make the
    status actionable.
    """
    headers = getattr(exc, "headers", None)
    code = code_for_status(exc.status_code)
    if code is ErrorCode.INTERNAL_ERROR:
        # Same reasoning as the unhandled-exception handler: a 5xx detail is not
        # guaranteed to be rider-safe, so it is logged rather than returned.
        logger.error(
            "HTTPException %s on %s: %s",
            exc.status_code,
            _endpoint(request),
            exc.detail,
        )
        return envelope_response(exc.status_code, code, INTERNAL_ERROR_MESSAGE, headers)

    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    message = detail or HTTPStatus(exc.status_code).phrase
    return envelope_response(exc.status_code, code, message, headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Anything that escaped a route -> a 500 envelope with a fixed message.

    `exc` is logged with its traceback and never reaches the body. A DB error's
    text alone can contain the database host and user.
    """
    logger.exception("Unhandled exception on %s", _endpoint(request), exc_info=exc)
    return envelope_response(
        HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Attach all four handlers to an app. The single entry point — `main.py` calls
    this once, and tests call it on a throwaway app to prove the handlers fire.
    """
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)  # type: ignore[arg-type]
