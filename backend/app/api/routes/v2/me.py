"""
The caller's own lists under ``/api/v2/me`` (decision-log Entry 29).

**Context.** Routes about the signed-in account rather than one trip. Today:
``GET /api/v2/me/trips``, the trips the caller is an active member of, which
the home and account screens list with a role badge
(``docs/api-contract.md``, "Endpoints: v2 (new)").

**How it works.** ``public-read`` limiter first, then ``require_session``
(``401`` without a valid session). The GET has a schema-excluded HEAD sibling
with the same limiter (Entry 11). ``tests/test_route_dependency_audit.py``
holds each route here to exactly ``require_session`` by name
(``ACCOUNT_SCOPED``).

**Related.** ``app/data/repositories/trips.py`` (``list_for_member``),
``app/models/trip.py`` (``MyTripOut``).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.responses import PUBLIC_READ_429, error_responses
from app.core.ratelimit import limit_public_read
from app.core.security import require_session
from app.core.sessions import SessionUser
from app.data.db import SessionDep
from app.data.repositories import trips as trips_repo
from app.models.trip import MyTripOut

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/trips",
    dependencies=[Depends(limit_public_read)],
    summary="The caller's trips",
    response_description="Every trip the caller is an active member of, with their role.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. If a session cookie was sent it is cleared "
            '(`Max-Age=0`). Carries `WWW-Authenticate: Cookie realm="bike-trip-journal"`.',
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** "Your trips": where a signed-in rider finds the trips they belong
to, private ones included (decision-log Entry 29).

**How it works.** Gated by `require_session` (401 without a valid session).
Lists the trips the caller has an **active** membership on, with `role`
`rider` or `leader`. Revoked and self-departed memberships are left out, and a
pending join request is not a membership (see `GET /api/v2/me/join-requests`).
Newest membership first, then by trip `id`. Not paged. An account with no trips
gets `200` with `[]`.

**Related APIs.** `GET /api/v2/trips/{tripId}` opens one; `POST /api/v2/trips`
creates one; `GET /api/v2/trips` is the public list.
""",
)
async def list_my_trips(
    user: Annotated[SessionUser, Depends(require_session)], session: SessionDep
) -> list[MyTripOut]:
    """The caller's active memberships, each as its trip and role."""
    return [
        MyTripOut(id=trip.id, name=trip.name, startDate=trip.start_date, role=trip.role)
        for trip in await trips_repo.list_for_member(session, user.user_id)
    ]


# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "/trips",
    list_my_trips,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)
