"""
Photo routes -- ``GET``/``POST /api/trips/{slug}/stops/{stop_id}/photos``.

Same thin-handler shape as ``stops.py``: the access gate resolves the trip
(``require_trip_access`` on reads, ``require_trip_writer`` on the upload), the
repository owns column names and builds statements. ``{stop_id}`` is
verified to belong to the resolved trip before any photo work runs -- a 404 if
not, preventing cross-trip data access.
"""

import asyncio
import io
from collections.abc import Callable, Coroutine
from datetime import datetime
from functools import partial
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.routing import APIRoute
from pydantic import AwareDatetime
from starlette.types import Message

from app.api.responses import (
    PATH_PARAMETERS_422,
    PUBLIC_READ_429,
    WRITES_429,
    error_responses,
)
from app.core.errors import ApiError
from app.core.jpeg import MAX_PHOTO_BYTES, JpegError, strip_metadata
from app.core.ratelimit import limit_public_read, limit_writes
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

PHOTO_NOT_JPEG_MESSAGE = (
    "This photo couldn't be saved: it isn't a complete JPEG image. Nothing was stored. "
    "Sending it again unchanged will keep failing."
)

PHOTO_TOO_LARGE_MESSAGE = (
    "This photo couldn't be saved: it is larger than 15 MiB. Nothing was stored. "
    "Sending it again unchanged will keep failing."
)

# The whole multipart request may be at most this long: the 15 MiB file part
# plus room for the form fields and multipart framing.
MAX_UPLOAD_REQUEST_BYTES = 16 * 1024 * 1024


class CappedBodyRoute(APIRoute):
    """
    An `APIRoute` that refuses a `POST` body over `MAX_UPLOAD_REQUEST_BYTES` with a 422.

    Set router-wide, but only the upload (`POST`) is capped: the list's `GET`
    and `HEAD` pass straight through, so they never answer with the photo
    message. (`@router.post` has no per-route `route_class` parameter.)

    **Why here and not in a dependency.** FastAPI reads and parses a multipart
    body (spooling the file to disk) *before* it solves any dependency, so
    `require_trip_writer` and `limit_writes` cannot stop a huge upload from being
    read. The route handler this class wraps is the first per-route code that
    runs, after CSRF and routing but before the body is touched:

    - a declared `Content-Length` over the cap is refused before a byte of the
      body is received;
    - a body with no usable `Content-Length` (chunked) is counted as it
      streams in, and the read stops at the first chunk that crosses the cap.

    The streamed case raises `HTTPException`, not `ApiError`: it is raised from
    inside FastAPI's body read, which re-raises `HTTPException` and turns any
    other exception into a `400`. Both render as the same `422
    VALIDATION_ERROR` envelope (`core/errors.py`).

    A request under the cap goes on to the gate, the limiter and the handler in
    their usual order; the handler enforces the 15 MiB limit on the file part
    itself (`_stripped_jpeg`).
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def capped_handler(request: Request) -> Response:
            if request.method != "POST":
                return await handler(request)

            declared = request.headers.get("content-length")
            # A non-numeric value never reaches here under uvicorn (h11 rejects
            # it); if one did, the streamed count below still caps the body.
            if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_REQUEST_BYTES:
                raise ApiError.validation(PHOTO_TOO_LARGE_MESSAGE)

            received = 0

            async def capped_receive() -> Message:
                nonlocal received
                message = await request.receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > MAX_UPLOAD_REQUEST_BYTES:
                        raise HTTPException(
                            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                            detail=PHOTO_TOO_LARGE_MESSAGE,
                        )
                return message

            return await handler(Request(request.scope, capped_receive))

        return capped_handler


router = APIRouter(
    prefix="/trips/{slug}/stops/{stop_id}/photos", tags=["photos"], route_class=CappedBodyRoute
)

STOP_NOT_FOUND_MESSAGE = (
    "This stop doesn't exist on this trip, so photos can't be listed or uploaded for it."
)

PHOTO_ID_CONFLICT_MESSAGE = (
    "This photo couldn't be saved: its id is already in use on another stop. Nothing was "
    "changed here. Sending it again unchanged will keep failing -- it needs a new id."
)

# A NUL byte in the form's `id`: no stored id can contain one.
PHOTO_ID_HAS_NUL_MESSAGE = (
    "This photo couldn't be saved: its id contains a NUL character, which no id can hold. "
    "Nothing was stored. Sending it again unchanged will keep failing -- it needs a new id."
)


async def _verified_stop(context: TripContext, stop_id: str, session) -> None:
    """Raise 404 if stop_id is not on this trip."""
    if not await stop_belongs_to_trip(session, context.trip.id, stop_id):
        raise ApiError.not_found(STOP_NOT_FOUND_MESSAGE)


async def _stripped_jpeg(file: UploadFile) -> bytes:
    """The upload's bytes with metadata removed; 422 if over 15 MiB or not a JPEG."""
    data = await file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise ApiError.validation(PHOTO_TOO_LARGE_MESSAGE)
    try:
        # Linear, but up to 15 MiB of Python-level walking: off the event loop.
        return await asyncio.to_thread(strip_metadata, data)
    except JpegError as exc:
        raise ApiError.validation(PHOTO_NOT_JPEG_MESSAGE) from exc


@router.get(
    "",
    dependencies=[Depends(limit_public_read)],
    summary="List a stop's photos",
    response_description="Every photo on this stop, each with a presigned URL.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: "No trip has this slug, or the stop id does not exist on "
            "this trip.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
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


router.add_api_route(
    "",
    list_photos,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


@router.post(
    "",
    dependencies=[Depends(limit_writes)],
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
                HTTPStatus.UNPROCESSABLE_ENTITY: "A required form field is missing or invalid; "
                "or the `file` part is not a complete JPEG (it must start `FF D8 FF` and walk "
                "cleanly to a Start-of-Scan) or is over 15 MiB (15,728,640 bytes); or the "
                "request's `Content-Length` is over 16 MiB, refused before the body is read; "
                "or the `id` contains a NUL character, which no stored id can hold. "
                "Never retried. Nothing was stored.",
                HTTPStatus.TOO_MANY_REQUESTS: WRITES_429,
            }
        ),
    },
    description="""
**Context.** This is how a photo gets into the journal. Multipart: form fields
(`id`, `takenAt`, and an optional, ignored `uploadedBy`) alongside the binary
`file` part. Only a signed-in, active member of the trip can upload; either slug
just locates the trip (decision-log Entry 29, contract default 21). Tasks
`t-photo-upload-endpoint`, `t-am-write-gate-legacy`, `t-am-photo-exif-strip`.

**How it works.** `require_trip_writer` checks the slug (404), the session (401)
and an active membership (403), in that order, before anything else — a replay
is not an exception to it. The one check ahead of it is the request-size cap: a
`Content-Length` over 16 MiB (or a body that streams past it) is a 422 before
the body is read. `{stop_id}` is verified to belong to the resolved trip.
The stored `uploadedBy` is the signed-in account's display name at upload time,
whatever the form sent, and `created_by` is the account.
The `id` form field decides the three-way idempotency branch: unseen -> 201,
same stop -> 200 replay, different stop -> 409. A replay is answered before the
file is looked at. Otherwise the file must be a JPEG of at most 15 MiB (422 if
not), and its metadata segments -- APP1-APP15 (EXIF, GPS, XMP, ICC) and COM --
are removed before storage; the image data is stored byte-for-byte. The
stripped file is stored in S3 with object key `{trip_id}/{stop_id}/{photo_id}`,
and only the key is persisted in the database.

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
    file: Annotated[
        UploadFile,
        File(
            description="The photo: a JPEG of at most 15 MiB. Its EXIF, XMP, ICC and comment "
            "segments are removed before it is stored."
        ),
    ],
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
    return await store_photo(session, context, stop_id, id, takenAt, file, response)


async def store_photo(
    session: SessionDep,
    context: TripWriterContext,
    stop_id: str,
    photo_id: str,
    taken_at: datetime,
    file: UploadFile,
    response: Response,
) -> PhotoOut:
    """
    The upload after the gate, shared by the legacy and v2 routes.

    Stop check (404), then the client id: a NUL byte is a 422 before any query
    (Postgres ``text`` cannot hold one; the lookup would be a 500). Then the
    three-way branch, the JPEG strip and the storage write, in that order, once
    for both surfaces. The request-size cap is not here: it has to run before
    the body is read, so it is the route class (``CappedBodyRoute``).
    """
    await _verified_stop(context, stop_id, session)
    if "\x00" in photo_id:
        raise ApiError.validation(PHOTO_ID_HAS_NUL_MESSAGE)

    # Three-way idempotency: check replay first (avoids uploading bytes twice)
    existing = await find_existing(session, stop_id, photo_id)
    if existing is not None:
        response.status_code = HTTPStatus.OK
        return existing

    # After the replay branch, so a replay never touches the bytes; before
    # anything is written, so a rejected upload leaves no object and no row.
    stripped = await _stripped_jpeg(file)

    # Cross-stop conflict check
    if await check_id_conflict(session, photo_id):
        raise ApiError.conflict(PHOTO_ID_CONFLICT_MESSAGE)

    # Upload to S3, then persist the DB row
    object_key = f"{context.trip.id}/{stop_id}/{photo_id}"

    # The stripped bytes, never the upload as received. boto3 switches to
    # multipart on its own above its size threshold. That is a memory choice,
    # not resumability: a photo is one idempotent request, retried whole under
    # the same client id by the offline queue: a replay is answered above with
    # no storage write, and the deterministic key means a retry after a failed
    # insert overwrites rather than duplicates (decision-log Entry 20). This
    # block is where "add resumable multipart" would be re-proposed -- read
    # Entry 20 first.
    s3 = get_s3_client()
    await asyncio.to_thread(
        partial(s3.upload_fileobj, io.BytesIO(stripped), BUCKET_NAME, object_key)
    )

    photo = await insert(
        session,
        stop_id,
        photo_id,
        # The account's name, never the form's `uploadedBy` (contract default 23).
        context.user.display_name,
        taken_at,
        object_key,
        created_by=context.user.user_id,
    )
    return photo
