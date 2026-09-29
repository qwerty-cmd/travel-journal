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

**Raise it through the classmethod constructors** — `ApiError.unauthenticated(...)`,
`ApiError.forbidden(...)`, `ApiError.not_found(...)`, `ApiError.conflict(...)`,
`ApiError.validation(...)`, `ApiError.rate_limited(...)`, `ApiError.internal(...)`
— never by passing a status and a code as separate
arguments. The contract's top-priority distinction is 403-vs-404 (spec Section
12, decision-log entry 6), and a two-argument call site makes the transposed
pair `(404, FORBIDDEN)` representable: a body that violates the contract,
produced by a typo, that no status-only or code-only assertion would catch. The
classmethods make it unrepresentable; `__init__` rejects it as a backstop for
anything that still constructs `ApiError` directly.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.headers import SECURITY_HEADERS
from app.models.common import ErrorCode, ErrorDetail, ErrorEnvelope

logger = logging.getLogger(__name__)

# The one message a rider is ever shown for a server fault. Deliberately fixed
# and content-free: the original exception text can carry a database host, a
# connection string, a SQL fragment or a file path, and `ErrorDetail.message` is
# documented as safe to display directly. The real exception is logged instead.
INTERNAL_ERROR_MESSAGE = "Something went wrong on our end. Please try again."

# Status -> contract code. Only the statuses the contract's eight endpoints
# actually return get a specific code; everything else (including a 500) is an
# INTERNAL_ERROR, because the contract defines exactly eight codes and inventing a
# ninth at runtime would produce a body the generated client cannot type.
#
# `409` is the one row here that is *endpoint* contract rather than framework
# level (decision-log Entry 14). `405` and `500` are produced by Starlette's
# routing and by anything that escapes a route — on any path, for reasons no
# endpoint declares — which is why they are deliberately not listed per-endpoint.
# A `409` is raised by our own handler code and is reachable only on the three
# create endpoints that accept a client-generated id, so the contract lists it on
# exactly those three rows and nowhere else.
#
# Adding this row is also what makes `ApiError(409, CONFLICT, ...)` constructible
# — `__init__` checks the pair against `code_for_status` — and that is the
# intended effect, not a side one.
#
# The flip side of keying on *status*: a bare `HTTPException(409)` also renders a
# well-formed CONFLICT envelope — carrying whatever `detail` it was given — without
# passing through `ApiError.conflict`, where the "no value from the conflicting
# record" rule is held. The envelope looks right, so nothing at runtime would
# notice. The guard is therefore at the raise site, not here and not in
# `http_exception_handler`: `tests/test_bare_409_raise_audit.py` fails the suite
# if any module under `app/` raises an `HTTPException` with a 409 (or with a
# status it cannot resolve statically). Do not "fix" this by dropping the row.
#
# `401` and `429` (decision-log Entry 29) are endpoint contract too: raised by our
# own session gate and rate limiters, listed per endpoint wherever reachable. Each
# carries a header the status is not actionable without — `WWW-Authenticate`
# (RFC 9110 requires it on a 401) and `Retry-After` — which is why they are raised
# through `ApiError.unauthenticated` / `ApiError.rate_limited`, the constructors
# that attach them.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    HTTPStatus.UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
    HTTPStatus.FORBIDDEN: ErrorCode.FORBIDDEN,
    HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTPStatus.CONFLICT: ErrorCode.CONFLICT,
    HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    HTTPStatus.UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
    HTTPStatus.TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
}

# The `WWW-Authenticate` value on every 401 (docs/api-contract.md, "Sessions").
# RFC 9110 makes the header a MUST on a 401; the `Cookie` scheme names how this
# API authenticates without being one a browser knows, so it never triggers the
# browser's own login dialog.
WWW_AUTHENTICATE = 'Cookie realm="bike-trip-journal"'


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

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
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
                f"ApiError.unauthenticated/.forbidden/.not_found/.conflict/.validation/"
                f".rate_limited/.internal instead."
            )
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # Response headers the handler sends with the envelope. Set only by the
        # constructors that owe one (401 -> WWW-Authenticate, 429 -> Retry-After).
        self.headers = headers

    @classmethod
    def unauthenticated(cls, message: str) -> ApiError:
        """
        401 — no valid session where one is required, or wrong signin/recover credentials.

        Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`, which RFC 9110
        requires on every 401. `message` is shown to the rider.
        """
        return cls(
            HTTPStatus.UNAUTHORIZED,
            ErrorCode.UNAUTHENTICATED,
            message,
            headers={"WWW-Authenticate": WWW_AUTHENTICATE},
        )

    @classmethod
    def forbidden(cls, message: str) -> ApiError:
        """403 — a valid slug used where it has no write permission. `message` is shown."""
        return cls(HTTPStatus.FORBIDDEN, ErrorCode.FORBIDDEN, message)

    @classmethod
    def not_found(cls, message: str) -> ApiError:
        """404 — no such trip/stop/bike/path. `message` is shown to the rider."""
        return cls(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, message)

    @classmethod
    def conflict(cls, message: str) -> ApiError:
        """
        409 — a client-generated id that already exists under a *different* parent.

        `message` is returned verbatim, and must contain **no value from the
        conflicting record**: not the other trip's slug, id or name, not the
        other stop's fields. A 409 already tells the caller the id exists
        somewhere; the message adds nothing to that. That boundary is held at the
        raise site, which does not read the conflicting row in the first place —
        this constructor only ever sees the string it is handed.
        """
        return cls(HTTPStatus.CONFLICT, ErrorCode.CONFLICT, message)

    @classmethod
    def validation(cls, message: str) -> ApiError:
        """
        422 — a request that passed schema validation but failed a rule we enforce.

        FastAPI's own schema failures never come through here; they arrive as
        `RequestValidationError`. This is for the checks Pydantic can't express.
        """
        return cls(HTTPStatus.UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_ERROR, message)

    @classmethod
    def rate_limited(cls, message: str, retry_after_seconds: int) -> ApiError:
        """
        429 — a rate limit or an account lockout. `message` is shown to the rider.

        Carries `Retry-After: <retry_after_seconds>`, which the offline queue waits
        out before retrying. The contract says whole seconds, at least 1; a value
        below 1 is a caller bug (a `Retry-After: 0` would have the queue hammer the
        limiter), so it raises `ValueError` here rather than reaching the wire.
        """
        if retry_after_seconds < 1:
            raise ValueError(
                f"retry_after_seconds must be at least 1, not {retry_after_seconds}. "
                f"Round the wait up to whole seconds before raising."
            )
        return cls(
            HTTPStatus.TOO_MANY_REQUESTS,
            ErrorCode.RATE_LIMITED,
            message,
            headers={"Retry-After": str(retry_after_seconds)},
        )

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
#      CLOSED ahead of cutover (Container Apps ships stdout to Log Analytics) by
#      `t-access-log-slug-exposure`: `backend/log_config/uvicorn.json`, passed
#      as `--log-config` at startup, filters the slug segment out of access and
#      error lines. Not by silencing access logs (that would cost
#      status/method/latency on every request), and not by an app module
#      reconfiguring uvicorn's logger at import time (the silently-inert shape
#      Entry 7(b) records). A server started without `--log-config` still logs
#      slugs.
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

    return envelope_response(exc.status_code, exc.code, exc.message, exc.headers)


def _format_validation_errors(errors: list[dict[str, Any]]) -> str:
    """
    A short, rider-readable summary of what failed validation.

    Only the field location and pydantic's own message are used — never the
    submitted `input`, which is user data we have no reason to echo back.

    One error type gets a fixed message instead of its own: `json_invalid`, the
    body that never parsed. FastAPI builds it with `loc=("body", e.pos)`, and
    that `pos` is a **byte offset** — so the generic branch below renders it as
    `"1: JSON decode error"`, a field path naming a field called `1`. There is
    no field to name when the body isn't JSON, so we don't invent one. Handled
    here rather than per-route because every body-taking route funnels through
    this function.

    A second type gets the same treatment: `missing` at `loc == ("body",)`, the
    **empty** body. The generic branch strips `"body"` from the location and
    leaves pydantic's bare `Field required` — a message with no subject. The
    condition is exactly that pair; a `missing` on a named field (`("body",
    "lat")`) still renders as `lat: Field required`.
    """
    parts: list[str] = []
    for error in errors:
        if error.get("type") == "json_invalid":
            parts.append("The request body could not be read as JSON.")
            continue
        if error.get("type") == "missing" and tuple(error.get("loc", ())) == ("body",):
            parts.append("The request body is missing.")
            continue
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

    Starlette routes an `Exception` handler to `ServerErrorMiddleware`, which
    sits outside user middleware — so `SecurityHeadersMiddleware` never sees
    this response, and the headers are set here instead.
    """
    logger.exception("Unhandled exception on %s", _endpoint(request), exc_info=exc)
    return envelope_response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        INTERNAL_ERROR_MESSAGE,
        dict(SECURITY_HEADERS),
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
