"""
Rider writes under ``/api/v2/trips/{tripId}`` (decision-log Entry 29).

**Context.** The v2 counterparts of the four legacy slug writes: add a stop,
upload a photo, add a bike, edit a bike. They "behave exactly like their legacy
counterparts, except for the locator and the gate" (``docs/api-contract.md``,
"Rider writes (v2)").

**How it works.**

- **Rate limit first.** ``limit_writes`` is each route's first dependency, so
  a flood is refused before the gate touches the database.
- **Gate.** ``require_trip_writer_by_id`` (``app/core/security.py``): session
  (``401`` for every trip id, existing or not) → trip located (public, or the
  caller has any membership row; else the reader's byte-identical ``404``) →
  active membership (``403``). It runs before any replay lookup.
- **Everything after the gate is the legacy code.** Each handler hands off to
  the shared helper its legacy route uses (``store_stop``, ``store_photo``,
  ``store_bike``, ``apply_bike_patch``), so the three-way idempotency branch,
  the NUL-byte guard on client ids, the JPEG strip and the storage write exist
  once and cannot drift between surfaces.
- **Photo upload.** Its own router with ``route_class=CappedBodyRoute``, the
  legacy upload's class, so a ``Content-Length`` over 16 MiB is a ``422``
  before the body is read. The form is ``id``, ``takenAt`` and ``file`` only.

**Related.** ``app/api/routes/stops.py``, ``photos.py`` and ``bikes.py`` (the
legacy writes and the shared helpers); ``app/api/routes/v2/trips.py`` (the v2
reads of what these write).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Response, UploadFile
from pydantic import AwareDatetime

from app.api.responses import error_responses
from app.api.routes.bikes import apply_bike_patch, store_bike
from app.api.routes.photos import CappedBodyRoute, store_photo
from app.api.routes.stops import store_stop
from app.core.ratelimit import limit_writes
from app.core.security import TripWriterContext, require_trip_writer_by_id
from app.data.db import SessionDep
from app.models.bike import BikeCreate, BikeOut, BikePatch
from app.models.photo import PhotoOut
from app.models.stop import StopCreate, StopOut

router = APIRouter(prefix="/trips/{tripId}", tags=["trips"])
# The upload alone, so the body cap applies to it and to nothing else here
# (`@router.post` takes no per-route `route_class`).
photo_router = APIRouter(prefix="/trips/{tripId}", tags=["trips"], route_class=CappedBodyRoute)

WriterDep = Annotated[TripWriterContext, Depends(require_trip_writer_by_id)]

V2_WRITES_429 = (
    "The `writes` limit: 600 requests an hour per account, or per client address for a "
    "request with no session. Checked before the session, the trip and the membership, so "
    "nothing was written. Retry after `Retry-After` seconds."
)
V2_WRITE_401 = (
    "No valid session: none sent, expired, signed out or revoked, or the account is disabled. "
    "Checked **before** the trip is looked up, so the answer is the same for a public trip, a "
    "private trip and a trip id that doesn't exist. If a session cookie was sent it is cleared "
    "(`Max-Age=0`). The offline queue pauses on this and resumes after sign-in. Nothing was "
    "written."
)
V2_WRITE_403 = (
    "Signed in, and the trip is visible to the caller (public, or they have a membership row "
    'on it), but they have no active membership: "You\'re not a rider on this trip." for a '
    'non-member of a public trip, "You\'re no longer a rider on this trip." for a revoked '
    "member. Nothing was written, and a replay of an already-stored id is refused the same way."
)
V2_TRIP_404 = (
    "No trip has this id, **or** the trip is private and the caller has no membership row on "
    "it. The two are byte-identical, as on the v2 reads, so a trip id can't be used to learn "
    "whether a private trip exists."
)
V2_GATE = """`limit_writes` runs first, then `require_trip_writer_by_id`: a valid
session (401, for every trip id alike), then the trip (404 if it doesn't exist,
or if it is private and the caller has no membership row on it -- the same 404
the v2 reads give), then an active membership as rider or leader (403). The gate
runs before any replay lookup."""


@router.post(
    "/stops",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.CREATED,
    summary="Add a stop to a trip",
    response_description="The stop as stored -- the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": StopOut,
            "description": "**A replay, not a second stop.** This id is already a stop on "
            "this trip, so nothing was created and the **stored** record is returned "
            "unchanged, even where this request's body differs from it.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
                HTTPStatus.FORBIDDEN: V2_WRITE_403,
                HTTPStatus.NOT_FOUND: V2_TRIP_404,
                HTTPStatus.CONFLICT: "The `id` in the body already belongs to a stop on a "
                "**different** trip. Nothing was created, and nothing about the conflicting "
                "record is disclosed. Never-retry: the stop needs a new id.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation -- a "
                "missing or mistyped field, a `locationSource` outside `gps`/`manual`, "
                "coordinates out of range, or an `arrivedAt` with **no UTC offset** -- or its "
                "`id` contains a NUL character, which no stored id can hold.",
                HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
            }
        ),
    },
    description=f"""
**Context.** How a stop gets into the journal on the v2 surface, located by trip
id (decision-log Entry 29). The same write as `POST /api/trips/{{slug}}/stops`;
only the locator and the gate differ. Task `t-am-v2-rider-writes`.

**How it works.** {V2_GATE} `StopCreate.id` (client-generated at capture time)
then decides one of three branches, matched on `(trip, id)`: unseen -> `201`;
already on this trip -> `200` with the stored record (a replay changes nothing);
on a different trip -> `409`, nothing disclosed. `created_by` is the signed-in
account. `arrivedAt` must carry a UTC offset.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/stops` lists what this writes;
`POST /api/v2/trips/{{tripId}}/stops/{{stopId}}/photos` attaches photos to it.
""",
)
async def create_stop(
    context: WriterDep, session: SessionDep, stop: StopCreate, response: Response
) -> StopOut:
    """Create the stop, or hand back the one this id already named on this trip."""
    return await store_stop(session, context, stop, response)


@photo_router.post(
    "/stops/{stopId}/photos",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.CREATED,
    summary="Upload a photo to a stop",
    response_description="The photo as stored -- the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": PhotoOut,
            "description": "**A replay, not a second upload.** This id is already a photo on "
            "this stop, so nothing was created or stored and the **stored** record is returned.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
                HTTPStatus.FORBIDDEN: V2_WRITE_403,
                HTTPStatus.NOT_FOUND: V2_TRIP_404 + " Also: the stop does not exist on this trip.",
                HTTPStatus.CONFLICT: "The `id` field already belongs to a photo on a "
                "**different** stop. Nothing was stored.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "A required form field is missing or invalid "
                "(`takenAt` with no UTC offset included); or the `id` contains a NUL character; "
                "or the `file` part is not a complete JPEG (it must start `FF D8 FF` and walk "
                "cleanly to a Start-of-Scan) or is over 15 MiB (15,728,640 bytes); or the "
                "request's `Content-Length` is over 16 MiB, refused before the body is read. "
                "Never retried. Nothing was stored.",
                HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
            }
        ),
    },
    description=f"""
**Context.** How a photo gets into the journal on the v2 surface. Multipart: the
form fields `id` and `takenAt` alongside the binary `file` part, and nothing
else -- the uploader's name comes from the account. The same write as
`POST /api/trips/{{slug}}/stops/{{stop_id}}/photos`; only the locator and the
gate differ. Task `t-am-v2-rider-writes`.

**How it works.** The one check ahead of everything is the request-size cap: a
`Content-Length` over 16 MiB (or a body that streams past it) is a 422 before
the body is read. Then {V2_GATE} `{{stopId}}` must be a stop on this trip (404).
The `id` form field decides the three-way branch, matched on `(stop, id)`:
unseen -> 201; same stop -> 200 replay, answered before the file is looked at;
different stop -> 409. Otherwise the file must be a JPEG of at most 15 MiB (422
if not); its APP1-APP15 (EXIF, GPS, XMP, ICC) and COM segments are removed and
the stripped bytes are stored under `{{tripId}}/{{stopId}}/{{id}}`. `uploadedBy`
on the result is the account's display name at upload time.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/stops/{{stopId}}/photos` lists
what this writes; `POST /api/v2/trips/{{tripId}}/stops` creates the stop.
""",
)
async def upload_photo(
    context: WriterDep,
    session: SessionDep,
    stop_id: Annotated[
        str,
        Path(
            alias="stopId",
            description="The id of the stop the photo attaches to. A stop on another trip is "
            "answered exactly as a stop that doesn't exist: 404.",
        ),
    ],
    response: Response,
    # Inline `Form(...)` params, not a form model: a model binding `UploadFile`
    # makes the OpenAPI body `x-www-form-urlencoded` (decision-log Entries 16, 24).
    id: Annotated[
        str,
        Form(
            description="Client-generated UUID4, assigned when the photo is captured "
            "(possibly offline, before upload succeeds). Replaying the same id on the same "
            "stop returns the existing photo (200) instead of storing a duplicate. See "
            "docs/api-contract.md 'Idempotency'."
        ),
    ],
    # `AwareDatetime`, never `datetime`: a naive value would be resolved in the
    # host's local zone by asyncpg (contract, "`takenAt` on the photo upload form
    # must be timezone-aware").
    takenAt: Annotated[
        AwareDatetime,
        Form(
            description="When the photo was taken, as a timezone-aware ISO 8601 instant: the "
            "capture time on the device, not the time the upload reached the server. The UTC "
            "offset is **required** -- a naive value is rejected with 422 / VALIDATION_ERROR. "
            "The generated client cannot catch this; only the server rejects it."
        ),
    ],
    file: Annotated[
        UploadFile,
        File(
            description="The photo: a JPEG of at most 15 MiB. Its EXIF, XMP, ICC and comment "
            "segments are removed before it is stored."
        ),
    ],
) -> PhotoOut:
    """Store the photo, or hand back the one this id already named on this stop."""
    return await store_photo(session, context, stop_id, id, takenAt, file, response)


@router.post(
    "/bikes",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.CREATED,
    summary="Add a bike to a trip",
    response_description="The bike as stored -- the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": BikeOut,
            "description": "**A replay, not a second bike.** This id is already a bike on "
            "this trip, so nothing was created and the **stored** record is returned "
            "unchanged.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
                HTTPStatus.FORBIDDEN: V2_WRITE_403,
                HTTPStatus.NOT_FOUND: V2_TRIP_404,
                HTTPStatus.CONFLICT: "The `id` in the body already belongs to a bike on a "
                "**different** trip. Nothing was created, nothing about the conflicting record "
                "is disclosed.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation, or its "
                "`id` contains a NUL character, which no stored id can hold.",
                HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
            }
        ),
    },
    description=f"""
**Context.** Registers a bike on a trip, located by trip id. The same write as
`POST /api/trips/{{slug}}/bikes`; only the locator and the gate differ. Task
`t-am-v2-rider-writes`.

**How it works.** {V2_GATE} `BikeCreate.id` then decides the three-way branch,
matched on `(trip, id)`: unseen -> 201; already on this trip -> 200 with the
stored record; on a different trip -> 409, nothing disclosed. `created_by` is
the signed-in account.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/bikes` lists bikes;
`PATCH /api/v2/trips/{{tripId}}/bikes/{{bikeId}}` edits one.
""",
)
async def create_bike(
    context: WriterDep, session: SessionDep, bike: BikeCreate, response: Response
) -> BikeOut:
    """Create the bike, or hand back the one this id already named on this trip."""
    return await store_bike(session, context, bike, response)


@router.patch(
    "/bikes/{bikeId}",
    dependencies=[Depends(limit_writes)],
    summary="Update a bike on a trip",
    response_description="The bike after applying the patch.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: V2_WRITE_401,
            HTTPStatus.FORBIDDEN: V2_WRITE_403,
            HTTPStatus.NOT_FOUND: V2_TRIP_404 + " Also: no bike with this id exists on the trip.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation, an explicit "
            "`null` included.",
            HTTPStatus.TOO_MANY_REQUESTS: V2_WRITES_429,
        }
    ),
    description=f"""
**Context.** Partial update of a bike on a trip, located by trip id. The same
write as `PATCH /api/trips/{{slug}}/bikes/{{id}}`; only the locator and the gate
differ. Task `t-am-v2-rider-writes`.

**How it works.** {V2_GATE} Only the fields present in the body change; an
explicit `null` is a 422, and omitting a field is how "no change" is spelled.
Last-write-wins: no conflict detection.

**Related APIs.** `POST /api/v2/trips/{{tripId}}/bikes` creates the bike;
`GET /api/v2/trips/{{tripId}}/bikes` returns the current state.
""",
)
async def patch_bike(
    context: WriterDep,
    session: SessionDep,
    bike_id: Annotated[
        str,
        Path(
            alias="bikeId",
            description="The bike's id. A bike on another trip is answered exactly as a bike "
            "that doesn't exist: 404.",
        ),
    ],
    body: BikePatch,
) -> BikeOut:
    """Apply a partial update to a bike on this trip."""
    return await apply_bike_patch(session, context, bike_id, body)
