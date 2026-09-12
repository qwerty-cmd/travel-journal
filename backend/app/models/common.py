from enum import StrEnum

from pydantic import BaseModel, Field

# The API contract's single error shape — every non-2xx response from this API
# uses this envelope, including FastAPI's own validation errors (wired via a
# global exception handler in Week 2's build, not here — this is the shape,
# not the wiring). Keeps Kubb's generated error handling uniform across every
# endpoint instead of one shape per failure type.


class ErrorCode(StrEnum):
    FORBIDDEN = "FORBIDDEN"  # viewer slug used on a write endpoint
    NOT_FOUND = "NOT_FOUND"  # slug/trip/stop/bike doesn't exist
    VALIDATION_ERROR = "VALIDATION_ERROR"  # request body failed schema validation


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str = Field(description="Human-readable — safe to show a rider directly.")


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
