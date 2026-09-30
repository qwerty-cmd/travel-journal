"""
Photo routes -- ``GET``/``POST /api/trips/{slug}/stops/{stop_id}/photos``.

Same thin-handler shape as ``stops.py``: the access gate resolves the trip
(``require_trip_access`` on reads, ``require_trip_writer`` on the upload), the
repository owns column names and builds statements. ``{stop_id}`` is
verified to belong to the resolved trip before any photo work runs -- a 404 if
not, preventing cross-trip data access.
"""

import asyncio
from functools import partial
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import AwareDatetime

from app.api.responses import PATH_PARAMETERS_422, error_responses
from app.core.errors import ApiError
from app.core.security import (
    TripContext,
    TripWriterContext,
    require_trip_access,
    require_trip_writer,
)
from app.data.db import SessionDep
from app.data.repositories.photos import (
    check_id_conflict,
    find_existing,
    insert,
    list_by_stop,
    stop_belongs_to_trip,
)
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
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: "No trip has this slug, or the stop id does not exist on "
            "this trip.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
        }
    ),
    description="""
**Context.** Photos attached to a single stop, each carrying a presigned URL
into S3-compatible storage. The URL is generated fresh per request and never
stored in the database. A stop with no photos is a `200` with `[]`.

**How it works.** `{slug}` is resolved by `require_trip_access` (either slug
accepted). `{stop_id}` is then verified to belong to the resolved trip -- a 404
if it does not. Photos are fetched by `data/repositories/photos.list_by_stop`.

**Related APIs.** `GET /api/trips/{slug}/stops` for the stop list,
`POST /api/trips/{slug}/stops/{stop_id}/photos` to upload a photo (active
members only).
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
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
                "revoked, or the account is disabled. Checked after the slug and before "
                "membership. If a session cookie was sent it is cleared (`Max-Age=0`). The "
                "offline queue pauses on this and resumes after sign-in. Nothing was stored.",
                HTTPStatus.FORBIDDEN: "Signed in, and the slug located the trip, but the account "
                'has no active membership on it: "You\'re not a rider on this trip." for a '
                'non-member, "You\'re no longer a rider on this trip." for a revoked one. The '
                "slug grants nothing, whichever one it is. Nothing was stored, and a replay of an "
                "already-stored id is refused the same way.",
                HTTPStatus.NOT_FOUND: "No trip has this slug, or the stop id does not exist on "
                "this trip.",
                HTTPStatus.CONFLICT: "The `id` field already belongs to a photo on a "
                "**different** stop.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "A required form field is missing or invalid.",
            }
        ),
    },
    description="""
**Context.** This is how a photo gets into the journal. Multipart: form fields
(`id`, `takenAt`, and an optional, ignored `uploadedBy`) alongside the binary
`file` part. Only a signed-in, active member of the trip can upload; either slug
just locates the trip (decision-log Entry 29, contract default 21). Tasks
`t-photo-upload-endpoint`, `t-am-write-gate-legacy`.

**How it works.** `require_trip_writer` checks the slug (404), the session (401)
and an active membership (403), in that order, before anything else — a replay
is not an exception to it. `{stop_id}` is verified to belong to the resolved trip.
The stored `uploadedBy` is the signed-in account's display name at upload time,
whatever the form sent, and `created_by` is the account.
The `id` form field decides the three-way idempotency branch: unseen -> 201,
same stop -> 200 replay, different stop -> 409. The file is stored in S3 with
object key `{trip_id}/{stop_id}/{photo_id}`, and only the key is persisted in
the database.

**Related APIs.** `GET /api/trips/{slug}/stops/{stop_id}/photos` lists what this
endpoint writes, `POST /api/trips/{slug}/stops` creates the stop this photo
attaches to.
""",
)
async def upload_photo(
    context: Annotated[TripWriterContext, Depends(require_trip_writer)],
    session: SessionDep,
    stop_id: str,
    response: Response,
    # Inline `Form(...)` params on purpose; don't fold them into a Pydantic form model.
    # A model carrying `UploadFile` as a field does bind on FastAPI 0.141.1, but the
    # OpenAPI document then declares `application/x-www-form-urlencoded`, not the
    # `multipart/form-data` the contract promises, and the Kubb client follows it.
    # Decision-log Entry 24 (won't-fix) and Entry 16.
    id: Annotated[
        str,
        Form(
            description="Client-generated UUID4, assigned when the photo is captured "
            "(possibly offline, before upload succeeds). Replaying the same id -- same "
            "photo retried after a dropped connection -- returns the existing photo (200) "
            "instead of storing a duplicate. See docs/api-contract.md 'Idempotency'."
        ),
    ],
    # `AwareDatetime` and a bare `datetime` emit the *same* JSON Schema, so the aware type
    # looks free in a spec diff. It is not: SQLAlchemy's asyncpg dialect has no bind
    # processor, so a naive value reaches asyncpg untouched and `timestamptz_encode` calls
    # `obj.astimezone(utc)` -- resolving it in the *host process's* local zone. Measured:
    # `2026-06-14T10:00` becomes `02:00Z` on a UTC+8 host and `10:00Z` under the container.
    # Same input, different instant, decided by where the API runs.
    # See docs/api-contract.md, "`takenAt` on the photo upload form must be timezone-aware".
    # Do not relax this to `datetime`.
    takenAt: Annotated[
        AwareDatetime,
        Form(
            description="When the photo was taken, as a timezone-aware ISO 8601 instant. "
            "Captured on the device, which may have been offline, so it is the capture time "
            "and not the time the upload reached the server. The UTC offset is **required** "
            "-- a naive value (no offset) is rejected with 422 / VALIDATION_ERROR rather than "
            "assumed to be UTC or server-local, because a rider crossing timezones has no "
            "offset worth guessing. EXIF `DateTimeOriginal` is naive, so the client composes "
            "this field: pair it with `OffsetTimeOriginal` when present, otherwise apply the "
            "device's current offset. The generated client cannot catch this; only the server "
            "rejects it."
        ),
    ],
    file: Annotated[UploadFile, File(description="The photo file.")],
    # Last, because it is the only form field with a default. Declared rather than
    # dropped so queued uploads from before the upgrade, which still send it, stay
    # valid in the generated client's types; the value is never read.
    uploadedBy: Annotated[
        str | None,
        Form(
            description="Optional and ignored. Before accounts, the uploader's free-text "
            "display name; queued uploads from then still send it. The stored name is now the "
            "signed-in account's display name at upload time, whatever this field says."
        ),
    ] = None,
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

    # Streamed from the spooled upload rather than read into memory first; boto3
    # switches to multipart on its own above its size threshold. That is a memory
    # choice, not resumability: a photo is one idempotent request, retried whole
    # under the same client id by the offline queue: a replay is answered above
    # with no storage write, and the deterministic key means a retry after a
    # failed insert overwrites rather than duplicates (decision-log Entry 20). This
    # block is where "add resumable multipart" would be re-proposed -- read
    # Entry 20 first.
    s3 = get_s3_client()
    await asyncio.to_thread(partial(s3.upload_fileobj, file.file, BUCKET_NAME, object_key))

    photo = await insert(
        session,
        stop_id,
        id,
        # The account's name, never the form's `uploadedBy` (contract default 23).
        context.user.display_name,
        takenAt,
        object_key,
        created_by=context.user.user_id,
    )
    return photo
