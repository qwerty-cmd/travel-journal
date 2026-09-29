from enum import StrEnum

from pydantic import BaseModel, Field

# The API contract's single error shape — every non-2xx response from this API
# uses this envelope, including FastAPI's own validation errors. This module is
# the shape; the wiring that makes it true of *every* response lives in
# `app/core/errors.py` (`register_exception_handlers`, called from `main.py`).
# Keeps Kubb's generated error handling uniform across every endpoint instead of
# one shape per failure type.


class ErrorCode(StrEnum):
    """
    The complete set of error codes the API can return — eight, no more.

    A client (notably the offline queue, which decides retry-vs-never-retry from
    the body alone) can exhaustively match on these. Adding a code is a contract
    change; a handler must never invent one, which is why `core/errors.py` maps
    every unrecognised status onto INTERNAL_ERROR rather than improvising.
    UNAUTHENTICATED and RATE_LIMITED are the seventh and eighth, admitted by
    decision-log Entry 29 (the ruling Entries 6 and 14 require).
    """

    # 401: no valid session where one is required, or wrong signin/recover
    # credentials. The offline queue *pauses* on it ("Sign in to send N items")
    # rather than failing the item — an expired session is fixed by signing in.
    UNAUTHENTICATED = "UNAUTHENTICATED"
    # Signed in (or a CSRF-blocked cross-site request), but not permitted.
    FORBIDDEN = "FORBIDDEN"
    # No such trip/slug/stop/bike/request/member — or a private trip the caller
    # may not see, byte-identical to a nonexistent one.
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"  # request body failed schema validation
    # A create whose client-generated id already exists under a *different*
    # parent — another trip's stop, not a replay of this one (decision-log
    # Entry 14). Deliberately NOT VALIDATION_ERROR, which was the strongest
    # rejected option: the request is well-formed, every field validates and the
    # server is healthy, so labelling it a validation failure tells the rider and
    # the client they sent something malformed when they did not. Getting the
    # right retry behaviour out of a wrong label is a coincidence that breaks the
    # moment anything branches on `code` for a reason other than retry.
    # Never-retry for the offline queue: an id that belongs to another trip on
    # this attempt still belongs to it on every later one, so retrying is
    # guaranteed to produce the same 409.
    CONFLICT = "CONFLICT"
    # A 405 is a client mistake (wrong verb on a real path), not a server fault —
    # kept distinct from INTERNAL_ERROR so the queue can tell "never retry this
    # request as written" from "the server broke, retrying may work".
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    # 429: a rate limit or an account lockout. Always carries `Retry-After`; the
    # queue retries after that many seconds instead of failing the item.
    RATE_LIMITED = "RATE_LIMITED"
    # Server fault: an unhandled exception, or any status the contract doesn't
    # otherwise name. `message` is always a fixed generic string for this code —
    # exception text can carry a database host, credentials or a SQL fragment,
    # and `message` is documented as safe to show a rider directly.
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str = Field(description="Human-readable — safe to show a rider directly.")


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
