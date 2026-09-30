"""
Bike routes -- ``POST /api/trips/{slug}/bikes`` and
``PATCH /api/trips/{slug}/bikes/{id}``.

POST uses the same three-way idempotency branch as stops: unseen id -> 201,
same trip -> 200 replay, different trip -> 409.  PATCH is a plain partial
update, last-write-wins by design.  Both go through ``require_trip_writer``:
either slug locates the trip (404 on unknown), then a session (401), then an
active membership (403).
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.responses import error_responses
from app.core.errors import ApiError
from app.core.security import TripWriterContext, require_trip_writer
from app.data.db import SessionDep
from app.data.repositories.bikes import BikeIdOnAnotherTrip, create, patch
from app.models.bike import BikeCreate, BikeOut, BikePatch

# POST/PATCH /trips/{slug}/bikes -- add or edit a bike (active members only).
router = APIRouter(prefix="/trips/{slug}/bikes", tags=["bikes"])

ID_ALREADY_USED_MESSAGE = (
    "This bike couldn't be saved: its id is already in use on another trip. Nothing was "
    "changed here. Sending it again unchanged will keep failing -- it needs a new id."
)


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    summary="Add a bike to a trip",
    response_description="The bike as stored -- the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": BikeOut,
            "description": "**A replay, not a second bike.** This id is already a bike on "
            "this trip, so nothing was created and the **stored** record is returned "
            "unchanged. Task `t-bikes-create-endpoint`.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or revoked, or the account is "
                "disabled. Checked after the slug and before membership. If a session cookie was "
                "sent it is cleared (`Max-Age=0`). The offline queue pauses on this and resumes "
                "after sign-in. Nothing was written.",
                HTTPStatus.FORBIDDEN: "Signed in, and the slug located the trip, but the account has no active "
                'membership on it: "You\'re not a rider on this trip." for a non-member, '
                '"You\'re no longer a rider on this trip." for a revoked one. The slug grants '
                "nothing, whichever one it is. Nothing was written.",
                HTTPStatus.NOT_FOUND: "No trip has this slug.",
                HTTPStatus.CONFLICT: "The `id` in the body already belongs to a bike on a "
                "**different** trip. Nothing was created, nothing about the conflicting record "
                "is disclosed.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation.",
            }
        ),
    },
    description="""
**Context.** Bikes are registered per trip so the journal records who is riding
what. Only a signed-in, active member of the trip can add one; either slug just
locates the trip (decision-log Entry 29, contract default 21). The server sets
`created_by` from the session. Tasks `t-bikes-create-endpoint`,
`t-am-write-gate-legacy`.

**How it works.** Same three-way idempotency branch as `POST /trips/{slug}/stops`
(decision-log Entry 14): unseen id creates the bike (201), id already on this
trip is a replay (200, stored record returned unchanged), id on a different trip
is a 409 with nothing disclosed about the conflicting record. `require_trip_writer`
runs first: slug (404), session (401), active membership (403).

**Related APIs.** `GET /api/trips/{slug}` returns bikes in `TripOut.bikes`,
`PATCH /api/trips/{slug}/bikes/{id}` edits a bike created here.
""",
)
async def create_bike(
    context: Annotated[TripWriterContext, Depends(require_trip_writer)],
    session: SessionDep,
    bike: BikeCreate,
    response: Response,
) -> BikeOut:
    """Create the bike, or hand back the one this id already named on this trip."""
    try:
        created_bike, created = await create(
            session, context.trip.id, bike, created_by=context.user.user_id
        )
    except BikeIdOnAnotherTrip:
        raise ApiError.conflict(ID_ALREADY_USED_MESSAGE) from None

    if not created:
        response.status_code = HTTPStatus.OK

    return created_bike


@router.patch(
    "/{id}",
    summary="Update a bike on a trip",
    response_description="The bike after applying the patch.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or revoked, or the account is "
            "disabled. Checked after the slug and before membership. If a session cookie was "
            "sent it is cleared (`Max-Age=0`). The offline queue pauses on this and resumes "
            "after sign-in. Nothing was written.",
            HTTPStatus.FORBIDDEN: "Signed in, and the slug located the trip, but the account has no active "
            'membership on it: "You\'re not a rider on this trip." for a non-member, '
            '"You\'re no longer a rider on this trip." for a revoked one. The slug grants '
            "nothing, whichever one it is. Nothing was written.",
            HTTPStatus.NOT_FOUND: "No trip has this slug, or no bike with this id exists on "
            "the trip.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation.",
        }
    ),
    description="""
**Context.** Partial update of a bike registered on this trip. Only the fields
present in the request body are changed — absent fields stay as they are.
Active members only, through `require_trip_writer` (slug 404, session 401,
membership 403). Tasks `t-bikes-patch-endpoint`, `t-am-write-gate-legacy`.

**How it works.** Last-write-wins: no conflict detection, no ETags, no version
field. Two concurrent patches both succeed; whichever one the database sees
last is the one that sticks (spec Section 4).

**Related APIs.** `POST /api/trips/{slug}/bikes` creates the bike in the first
place. `GET /api/trips/{slug}` returns the current state in `TripOut.bikes`.
""",
)
async def patch_bike(
    context: Annotated[TripWriterContext, Depends(require_trip_writer)],
    session: SessionDep,
    id: str,
    body: BikePatch,
) -> BikeOut:
    """Apply a partial update to a bike on this trip."""
    updated = await patch(session, context.trip.id, id, body)
    if updated is None:
        raise ApiError.not_found("No bike with this id exists on this trip.")
    return updated
