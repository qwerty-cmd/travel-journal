"""
Bike routes -- ``POST /api/trips/{slug}/bikes`` and
``PATCH /api/trips/{slug}/bikes/{id}``.

POST uses the same three-way idempotency branch as stops: unseen id -> 201,
same trip -> 200 replay, different trip -> 409.  PATCH is a plain partial
update, last-write-wins by design.  Rider slug only (403 on viewer, 404 on
unknown).
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.errors import ApiError
from app.core.security import TripContext, require_rider_access
from app.data.db import SessionDep
from app.data.repositories.bikes import BikeIdOnAnotherTrip, create, patch
from app.models.bike import BikeCreate, BikeOut, BikePatch
from app.models.common import ErrorEnvelope

# POST /trips/{slug}/bikes -- add a bike (rider-slug only, 403 on viewer slug).
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
        HTTPStatus.FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "The slug resolved, but it is the trip's **viewer** slug.",
        },
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug.",
        },
        HTTPStatus.CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The `id` in the body already belongs to a bike on a **different** "
            "trip. Nothing was created, nothing about the conflicting record is disclosed.",
        },
        HTTPStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorEnvelope,
            "description": "The body failed schema validation.",
        },
    },
    description="""
**Context.** Bikes are registered per trip so the journal records who is riding
what. Rider slug only -- a viewer link can read bikes (via `GET /trips/{slug}`)
but not add one. Task `t-bikes-create-endpoint`.

**How it works.** Same three-way idempotency branch as `POST /trips/{slug}/stops`
(decision-log Entry 14): unseen id creates the bike (201), id already on this
trip is a replay (200, stored record returned unchanged), id on a different trip
is a 409 with nothing disclosed about the conflicting record.

**Related APIs.** `GET /api/trips/{slug}` returns bikes in `TripOut.bikes`,
`PATCH /api/trips/{slug}/bikes/{id}` edits a bike created here.
""",
)
async def create_bike(
    context: Annotated[TripContext, Depends(require_rider_access)],
    session: SessionDep,
    bike: BikeCreate,
    response: Response,
) -> BikeOut:
    """Create the bike, or hand back the one this id already named on this trip."""
    try:
        created_bike, created = await create(session, context.trip.id, bike)
    except BikeIdOnAnotherTrip:
        raise ApiError.conflict(ID_ALREADY_USED_MESSAGE) from None

    if not created:
        response.status_code = HTTPStatus.OK

    return created_bike


@router.patch(
    "/{id}",
    summary="Update a bike on a trip",
    response_description="The bike after applying the patch.",
    responses={
        HTTPStatus.FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "The slug resolved, but it is the trip's **viewer** slug.",
        },
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug, or no bike with this id exists on the trip.",
        },
        HTTPStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorEnvelope,
            "description": "The body failed schema validation.",
        },
    },
    description="""
**Context.** Partial update of a bike registered on this trip. Only the fields
present in the request body are changed — absent fields stay as they are.
Rider slug only. Task `t-bikes-patch-endpoint`.

**How it works.** Last-write-wins: no conflict detection, no ETags, no version
field. Two concurrent patches both succeed; whichever one the database sees
last is the one that sticks (spec Section 4).

**Related APIs.** `POST /api/trips/{slug}/bikes` creates the bike in the first
place. `GET /api/trips/{slug}` returns the current state in `TripOut.bikes`.
""",
)
async def patch_bike(
    context: Annotated[TripContext, Depends(require_rider_access)],
    session: SessionDep,
    id: str,
    body: BikePatch,
) -> BikeOut:
    """Apply a partial update to a bike on this trip."""
    updated = await patch(session, context.trip.id, id, body)
    if updated is None:
        raise ApiError.not_found("No bike with this id exists on this trip.")
    return updated
