"""
OpenAPI `responses=` declarations for the error statuses, shared by every route.

Every non-2xx response the API sends is an `ErrorEnvelope` (`app/core/errors.py`
makes that true at runtime); this module makes the OpenAPI document say so. It
matters because Kubb generates the frontend client from that document: an
operation that declares no `422` gets FastAPI's auto-injected
`HTTPValidationError` instead, and the client would type errors as
`ErrorEnvelope | HTTPValidationError` — breaking the one-envelope premise the
offline queue relies on when it branches on `code` alone (decision-log Entry 17).

405 and 500 are deliberately absent: they are framework-level and not declared
per operation (`docs/api-contract.md`, error envelope section).
"""

from http import HTTPStatus

from app.models.common import ErrorEnvelope

ERROR_RESPONSES = {
    HTTPStatus.UNAUTHORIZED: {
        "model": ErrorEnvelope,
        "description": "No valid session where one is required. The response carries "
        '`WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
    },
    HTTPStatus.FORBIDDEN: {
        "model": ErrorEnvelope,
        "description": "The slug resolved, but it is the trip's viewer slug — read-only.",
    },
    HTTPStatus.NOT_FOUND: {
        "model": ErrorEnvelope,
        "description": "No trip has this slug, or a resource in the path does not exist on it.",
    },
    HTTPStatus.CONFLICT: {
        "model": ErrorEnvelope,
        "description": "The client-generated id already belongs to a different parent.",
    },
    HTTPStatus.UNPROCESSABLE_ENTITY: {
        "model": ErrorEnvelope,
        "description": "The request failed schema validation.",
    },
    # The one entry that declares a response header: the offline queue waits
    # `Retry-After` seconds before retrying a RATE_LIMITED item, so the generated
    # client should know the header is part of this response.
    HTTPStatus.TOO_MANY_REQUESTS: {
        "model": ErrorEnvelope,
        "description": "A rate limit or an account lockout. Retry after `Retry-After` seconds.",
        "headers": {
            "Retry-After": {
                "description": "Whole seconds to wait before retrying; always at least 1.",
                "schema": {"type": "integer", "minimum": 1},
            }
        },
    },
}

# The 422 on a read: these operations have no body, so only a path parameter can
# fail validation — and with every path parameter typed as a plain string, none
# currently can. Declared anyway so FastAPI does not inject `HTTPValidationError`.
PATH_PARAMETERS_422 = (
    "A path parameter failed validation. This operation takes no request body, so the "
    "path is the only thing that can fail; its path parameters are plain strings, so in "
    "practice it does not return this today."
)


# The 429 descriptions for the two limiters shared by many routes
# (`app/core/ratelimit.py`; contract, "Rate limits and lockout").
PUBLIC_READ_429 = (
    "The `public-read` limit: 120 requests a minute per client address. Retry after "
    "`Retry-After` seconds."
)
WRITES_429 = (
    "The `writes` limit: 600 requests an hour per account, or per client address for a "
    "request with no session. Checked before the slug, the session and the membership, so "
    "nothing was written. Retry after `Retry-After` seconds."
)


def error_responses(descriptions: dict[HTTPStatus, str]) -> dict[HTTPStatus, dict]:
    """
    Pick statuses from `ERROR_RESPONSES`, each with the route's own description.

    The route's description replaces the generic one; the model always comes from
    the constant, so no route can declare a non-2xx with any other schema.
    """
    return {
        status: {**ERROR_RESPONSES[status], "description": description}
        for status, description in descriptions.items()
    }
