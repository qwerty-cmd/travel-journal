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
    The complete set of error codes the API can return — five, no more.

    A client (notably the offline queue, which decides retry-vs-never-retry from
    the body alone) can exhaustively match on these. Adding a code is a contract
    change; a handler must never invent one, which is why `core/errors.py` maps
    every unrecognised status onto INTERNAL_ERROR rather than improvising.
    """

    FORBIDDEN = "FORBIDDEN"  # viewer slug used on a write endpoint
    NOT_FOUND = "NOT_FOUND"  # slug/trip/stop/bike doesn't exist
    VALIDATION_ERROR = "VALIDATION_ERROR"  # request body failed schema validation
    # A 405 is a client mistake (wrong verb on a real path), not a server fault —
    # kept distinct from INTERNAL_ERROR so the queue can tell "never retry this
    # request as written" from "the server broke, retrying may work".
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
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
