"""
Photo routes -- ``GET``/``POST /api/trips/{slug}/stops/{stop_id}/photos``.

Same thin-handler shape as ``stops.py``: the slug dependency resolves the trip,
the repository owns column names and builds statements. ``{stop_id}`` is
verified to belong to the resolved trip before any photo work runs -- a 404 if
not, preventing cross-trip data access.
"""

import asyncio
from datetime import datetime
from functools import partial
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from app.core.errors import ApiError
from app.core.security import TripContext, require_rider_access, require_trip_access
from app.data.db import SessionDep
from app.data.repositories.photos import (
    check_id_conflict,
    find_existing,
    insert,
    list_by_stop,
    stop_belongs_to_trip,
)
from app.models.common import ErrorEnvelope
from app.models.photo import PhotoOut
from app.storage.s3_client import BUCKET_NAME, get_s3_client

router = APIRouter(prefix="/trips/{slug}/stops/{stop_id}/photos", tags=["photos"])

STOP_NOT_FOUND_MESSAGE = (
    "This stop doesn't exist on this trip, so photos can't be listed or uploaded for it."
)

PHOTO_ID_CONFLICT_MESSAGE = (
    "This photo couldn't be saved: its id is already in use on another stop. Nothing was "
    "changed here. Sending it again unchanged will keep failing -- it needs a new id."
)


async def _verified_stop(context: TripContext, stop_id: str, session) -> None:
    """Raise 404 if stop_id is not on this trip."""
    if not await stop_belongs_to_trip(session, context.trip.id, stop_id):
        raise ApiError.not_found(STOP_NOT_FOUND_MESSAGE)


@router.get(
    "",
    summary="List a stop's photos",
    response_description="Every photo on this stop, each with a presigned URL.",
    responses={
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug, or the stop id does not exist on this trip.",
        },
    },
    description="""
**Context.** Photos attached to a single stop, each carrying a presigned URL
into S3-compatible storage. The URL is generated fresh per request and never
stored in the database. A stop with no photos is a `200` with `[]`.

**How it works.** `{slug}` is resolved by `require_trip_access` (either slug
accepted). `{stop_id}` is then verified to belong to the resolved trip -- a 404
if it does not. Photos are fetched by `data/repositories/photos.list_by_stop`.

**Related APIs.** `GET /api/trips/{slug}/stops` for the stop list,
`POST /api/trips/{slug}/stops/{stop_id}/photos` to upload a photo (rider slug
only).
""",
)
async def list_photos(
    context: Annotated[TripContext, Depends(require_trip_access)],
    session: SessionDep,
    stop_id: str,
) -> list[PhotoOut]:
    await _verified_stop(context, stop_id, session)
    return await list_by_stop(session, stop_id)


router.add_api_route("", list_photos, methods=["HEAD"], include_in_schema=False)


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    summary="Upload a photo to a stop",
    response_description="The photo as stored -- the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": PhotoOut,
            "description": "**A replay, not a second upload.** This id is already a photo on "
            "this stop, so nothing was created and the **stored** record is returned.",
        },
        HTTPStatus.FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "The slug resolved, but it is the trip's **viewer** slug.",
        },
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug, or the stop id does not exist on this trip.",
        },
        HTTPStatus.CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The `id` field already belongs to a photo on a **different** stop.",
        },
        HTTPStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorEnvelope,
            "description": "A required form field is missing or invalid.",
        },
    },
    description="""
**Context.** This is how a photo gets into the journal. Multipart: form fields
(`id`, `uploadedBy`, `takenAt`) alongside the binary `file` part. Rider slug
only. Task `t-photo-upload-endpoint`.

**How it works.** `{slug}` is resolved by `require_rider_access` (403 on viewer,
404 on unknown). `{stop_id}` is verified to belong to the resolved trip.
`PhotoCreateForm.id` decides the three-way idempotency branch: unseen -> 201,
same stop -> 200 replay, different stop -> 409. The file is stored in S3 with
object key `{trip_id}/{stop_id}/{photo_id}`, and only the key is persisted in
the database.

**Related APIs.** `GET /api/trips/{slug}/stops/{stop_id}/photos` lists what this
endpoint writes, `POST /api/trips/{slug}/stops` creates the stop this photo
attaches to.
""",
)
async def upload_photo(
    context: Annotated[TripContext, Depends(require_rider_access)],
    session: SessionDep,
    stop_id: str,
    response: Response,
    id: Annotated[str, Form(description="Client-generated UUID4 for idempotency.")],
    uploadedBy: Annotated[str, Form(description="Display name of the uploader.")],
    takenAt: Annotated[datetime, Form(description="ISO 8601 timestamp when the photo was taken.")],
    file: Annotated[UploadFile, File(description="The photo file.")],
) -> PhotoOut:
    await _verified_stop(context, stop_id, session)

    # Three-way idempotency: check replay first (avoids uploading bytes twice)
    existing = await find_existing(session, stop_id, id)
    if existing is not None:
        response.status_code = HTTPStatus.OK
        return existing

    # Cross-stop conflict check
    if await check_id_conflict(session, id):
        raise ApiError.conflict(PHOTO_ID_CONFLICT_MESSAGE)

    # Upload to S3, then persist the DB row
    object_key = f"{context.trip.id}/{stop_id}/{id}"
    file_bytes = await file.read()

    s3 = get_s3_client()
    await asyncio.to_thread(
        partial(s3.put_object, Bucket=BUCKET_NAME, Key=object_key, Body=file_bytes)
    )

    photo = await insert(
        session, stop_id, id, uploadedBy, takenAt, object_key,
    )
    return photo
