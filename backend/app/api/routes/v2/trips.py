"""
Trip routes under ``/api/v2/trips`` (decision-log Entry 29).

**Context.** The v2 surface locates trips by id, not by slug. This module
holds the anonymous Discover list, ``GET /api/v2/trips``, trip creation, a
leader's settings ``PATCH``, and the trip-scoped reads: the trip, its bikes, its stops, one stop's photos and its map
(``docs/api-contract.md``, "Endpoints: v2 (new)" and "Notes per endpoint (v2)").

**How it works.**

- **Gate.** The list is anonymous and declares no gate: it reads no session, so
  every caller gets the same page. It lists public trips only, and the
  repository applies that filter before the cursor, so no cursor can reach a
  private trip. ``tests/test_route_dependency_audit.py`` exempts it by route
  name (``list_public_trips``).
- **Rate limit.** ``public-read``, declared first so it answers before anything
  touches the database. The HEAD sibling declares it too.
- **Cursor.** Keyset pagination on ``(lastPublicStopAt, id)``. The cursor is
  base64url (no padding) of the compact JSON ``[lastPublicStopAt|null, id]`` of
  the previous page's last row. It is opaque to the client and checked strictly
  on the way in: it must decode *and* re-encode to exactly the string sent, so
  any edit that doesn't produce another well-formed position is a ``422``. A
  well-formed edited cursor only moves the position within the public list.

- **Trip-scoped reads.** Each declares ``require_trip_reader``
  (``app/core/security.py``): the trip exists and is public, or the caller is
  an active member, else a ``404`` byte-identical to a nonexistent id. Never a
  ``401``. The gate also decides the delay: ``None`` for an active member, the
  trip's ``public_delay_hours`` for anyone else. Handlers pass it to the
  repository filters unchanged, so stops, map pins, the trail and photos are
  all cut by the one rule (``stops.public_visibility``). Bikes are not delayed.
  Each has a schema-excluded HEAD sibling with the same limiter (Entry 11).

- **Create** (``POST /api/v2/trips``, ``t-am-trip-create``). ``trip-create``
  limiter first (a replay spends no token -- ``TripCreateRateLimit``), then
  ``require_session``. ``trips.create_for_user`` decides replay / id taken /
  lifetime cap / create under a lock on the creator's ``users`` row, and stores
  the trip and its leader membership in one transaction.
- **Settings** (``PATCH /api/v2/trips/{tripId}``). ``writes`` limiter, then
  ``require_trip_leader``. Only fields present in the body change.

**Related.** ``app/data/repositories/trips.py`` (``list_public``, ``get_stats``,
``create_for_user``, ``update_settings``),
``app/models/trip.py`` (``TripPageOut``, ``TripSummaryOut``, ``TripOut``), and
the legacy slug reads in ``app/api/routes/``, whose serialisation these reuse.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.api.routes.photos import STOP_NOT_FOUND_MESSAGE
from app.api.routes.trips import trip_out
from app.core.errors import ApiError
from app.core.ratelimit import limit_public_read, limit_trip_create, limit_writes
from app.core.security import (
    TripReaderContext,
    TripWriterContext,
    require_session,
    require_trip_leader,
    require_trip_reader,
)
from app.core.sessions import SessionUser
from app.data.db import SessionDep
from app.data.repositories import bikes as bikes_repo
from app.data.repositories import photos as photos_repo
from app.data.repositories import stops as stops_repo
from app.data.repositories import trips as trips_repo
from app.data.repositories.trips import CreateOutcome
from app.models.bike import BikeOut
from app.models.map import MapFeatureCollection
from app.models.photo import PhotoOut
from app.models.stop import StopOut
from app.models.trip import (
    TripCreate,
    TripOut,
    TripPageOut,
    TripPatch,
    TripSummaryOut,
    ViewerRole,
)

router = APIRouter(prefix="/trips", tags=["trips"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

BAD_CURSOR_MESSAGE = "cursor is not a valid page cursor; pass back a nextCursor unchanged."

Position = tuple[datetime | None, str]


def encode_cursor(position: Position) -> str:
    """base64url (unpadded) of the compact JSON ``[isoInstant|null, id]``."""
    at, trip_id = position
    raw = json.dumps([at.isoformat() if at is not None else None, trip_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> Position:
    """The position ``cursor`` encodes; ``ApiError.validation`` for anything else."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at_text, trip_id = json.loads(raw.decode("utf-8"))
        if not isinstance(trip_id, str) or not trip_id or "\x00" in trip_id:
            raise ValueError
        # JSON can spell a lone surrogate (`"\ud800"`), which is no UTF-8 text
        # Postgres could compare against: UnicodeEncodeError, so a 422.
        trip_id.encode("utf-8")
        if at_text is None:
            at = None
        elif isinstance(at_text, str):
            at = datetime.fromisoformat(at_text)
            if at.tzinfo is None:
                raise ValueError
        else:
            raise ValueError
        # Canonical only: whatever doesn't re-encode to exactly what was sent
        # was not issued by this server.
        if encode_cursor((at, trip_id)) != cursor:
            raise ValueError
        # The instant goes to the database in UTC. An offset that pushes it past
        # year 1 or 9999 overflows here (OverflowError), rather than in the
        # driver as a 500; anything that converts is inside timestamptz's range.
        if at is not None:
            at = at.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, RecursionError, binascii.Error, UnicodeError):
        raise ApiError.validation(BAD_CURSOR_MESSAGE) from None
    return at, trip_id


@router.get(
    "",
    dependencies=[Depends(limit_public_read)],
    summary="List public trips",
    response_description="One page of public trips, most recently active first.",
    responses=error_responses(
        {
            HTTPStatus.UNPROCESSABLE_ENTITY: "`limit` is outside 1-50, or `cursor` is not a "
            "`nextCursor` this endpoint issued.",
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** The Discover list: the public trips anyone can browse, with no
account (decision-log Entry 29). Private trips never appear.

**How it works.** Anonymous: a session, if sent, is ignored, and the response is
the same for every caller. Each item carries `riderCount` (active members,
leaders included) and `lastPublicStopAt`, the latest `arrivedAt` among stops
visible to the public — those with `arrivedAt <= now() - publicDelayHours` on the
server clock. Sorted by `lastPublicStopAt` descending with nulls last, then `id`
ascending; trips with no visible stop come last. Pages hold `limit` items
(default 20, 1-50). `nextCursor` is opaque; pass it back as `cursor` unchanged
for the next page. It is `null` on the last page. A cursor this endpoint did not
issue is a `422`.

**Related APIs.** `GET /api/v2/trips/{tripId}` for one trip,
`GET /api/v2/me/trips` for the trips the caller is a member of.
""",
)
async def list_public_trips(
    session: SessionDep,
    cursor: Annotated[
        str | None,
        Query(
            description="The `nextCursor` from the previous page, unchanged. Omit it for the "
            "first page. Anything this endpoint did not issue is rejected with 422 / "
            "VALIDATION_ERROR."
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description="How many trips per page, from 1 to 50 inclusive; defaults to 20. "
            "Anything else is rejected with 422 / VALIDATION_ERROR.",
        ),
    ] = DEFAULT_PAGE_SIZE,
) -> TripPageOut:
    """One page of public trips, fetched with one extra row to learn whether more follow."""
    after = decode_cursor(cursor) if cursor is not None else None
    rows = await trips_repo.list_public(session, after=after, limit=limit + 1)
    page = rows[:limit]
    next_cursor = (
        encode_cursor((page[-1].last_public_stop_at, page[-1].id)) if len(rows) > limit else None
    )
    return TripPageOut(
        items=[
            TripSummaryOut(
                id=row.id,
                name=row.name,
                startDate=row.start_date,
                riderCount=row.rider_count,
                lastPublicStopAt=row.last_public_stop_at,
            )
            for row in page
        ],
        nextCursor=next_cursor,
    )


# HEAD sibling: the same handler, schema-excluded (decision-log Entry 11).
router.add_api_route(
    "",
    list_public_trips,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


# --------------------------------------------------------------------------
# Create a trip: `require_session`, `trip-create`
# --------------------------------------------------------------------------

# Contract, "Rate limits and lockout": a lifetime cap of 20 trips created per
# user, answered 409.
LIFETIME_TRIP_CAP = 20

TRIP_ID_TAKEN_MESSAGE = (
    "This trip couldn't be created: its id is already in use. Nothing was changed. Sending it "
    "again unchanged will keep failing -- it needs a new id."
)
TRIP_CAP_MESSAGE = (
    "You've already created 20 trips, the most one account can create. Nothing was changed."
)
SESSION_401 = (
    "No valid session: none sent, expired, signed out or revoked, or the account is disabled. "
    "If a session cookie was sent it is cleared (`Max-Age=0`). The offline queue pauses on this "
    "and resumes after sign-in. Nothing was written."
)


@router.post(
    "",
    dependencies=[Depends(limit_trip_create)],
    status_code=HTTPStatus.CREATED,
    summary="Create a trip",
    response_description="The trip just created, with the caller as its leader (201).",
    responses={
        HTTPStatus.OK: {
            "model": TripOut,
            "description": "**A replay, not a second trip.** The caller created a trip with "
            "this id and is still an active member of it, so nothing was created and the "
            "**stored** trip is returned, even where this request's body differs from it. "
            "`viewer.role` is the caller's current role on it.",
        },
        **error_responses(
            {
                HTTPStatus.UNAUTHORIZED: SESSION_401,
                HTTPStatus.CONFLICT: "Either the `id` already names a trip that is not the "
                "caller's own replay -- another account's trip, a trip from before accounts, or "
                "one the caller created but has since left or been removed from -- or the caller "
                "has already created 20 trips (the lifetime cap). Nothing was created, and "
                "nothing about another trip is disclosed. Never-retry.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed validation: `id` is not a "
                "canonical lowercase UUID, `name` is empty or over 100 characters after "
                "trimming, `startDate` is not an ISO date, or `visibility` is not "
                "'public'/'private'.",
                HTTPStatus.TOO_MANY_REQUESTS: "The `trip-create` limit: 3 creates a day per "
                "account, or per client address for a request with no session. A replay of the "
                "caller's own trip spends no token and is never refused by it. Checked before "
                "the session is validated, so nothing was written. Retry after `Retry-After` "
                "seconds.",
            }
        ),
    },
    description="""
**Context.** How a signed-in rider starts a new trip (decision-log Entry 29). The
caller becomes its first leader. Trips created here have no slugs: they are
reached by id.

**How it works.** `limit_trip_create` runs first (3 a day per account; a replay
spends nothing), then `require_session` (401). `TripCreate.id` is
client-generated and must be a canonical lowercase UUID (422 otherwise). It then
decides one of three branches: an unseen id creates the trip -> `201`; an id the
caller created and is still an active member of is a replay -> `200` with the
stored trip; any other existing id -> `409`, nothing disclosed. A caller who has
already created 20 trips gets `409` for a new id. In one transaction the trip is
stored with `visibility` as sent (default `public`), `publicDelayHours` 24 and
no slugs, together with the caller's `leader` membership. Parallel creates by one
account are serialised, so the cap can't be overshot.

**Related APIs.** `GET /api/v2/me/trips` lists the caller's trips;
`PATCH /api/v2/trips/{tripId}` edits the settings; `GET /api/v2/trips/{tripId}`
reads it back.
""",
)
async def create_trip(
    user: Annotated[SessionUser, Depends(require_session)],
    session: SessionDep,
    body: TripCreate,
    response: Response,
) -> TripOut:
    """Create the trip with the caller as leader, or hand back the caller's own trip."""
    result = await trips_repo.create_for_user(
        session,
        trip_id=body.id,
        name=body.name,
        start_date=body.startDate,
        visibility=body.visibility.value,
        user_id=user.user_id,
        cap=LIFETIME_TRIP_CAP,
    )
    if result.outcome is CreateOutcome.ID_TAKEN:
        raise ApiError.conflict(TRIP_ID_TAKEN_MESSAGE)
    if result.outcome is CreateOutcome.CAP_REACHED:
        raise ApiError.conflict(TRIP_CAP_MESSAGE)
    if result.outcome is CreateOutcome.REPLAY:
        response.status_code = HTTPStatus.OK
    return await trip_out(session, result.trip, ViewerRole(result.role.value))


# --------------------------------------------------------------------------
# Trip settings: `require_trip_leader`, `writes`
# --------------------------------------------------------------------------


@router.patch(
    "/{tripId}",
    dependencies=[Depends(limit_writes)],
    summary="Update a trip's settings",
    response_description="The trip after applying the patch.",
    responses=error_responses(
        {
            HTTPStatus.UNAUTHORIZED: "No valid session: none sent, expired, signed out or "
            "revoked, or the account is disabled. Checked **before** the trip is looked up, so "
            "the answer is the same for a public trip, a private trip and a trip id that "
            "doesn't exist. If a session cookie was sent it is cleared (`Max-Age=0`). Nothing "
            "was changed.",
            HTTPStatus.FORBIDDEN: "Signed in, and the trip is visible to the caller (public, "
            "or they have a membership row on it), but they are not an active leader: "
            '"You\'re not a rider on this trip." for a non-member of a public trip, '
            '"You\'re no longer a rider on this trip." for a revoked member, "You\'re not a '
            'leader on this trip." for an active rider. Nothing was changed.',
            HTTPStatus.NOT_FOUND: "No trip has this id, **or** the trip is private and the "
            "caller has no membership row on it. The two are byte-identical, as on the v2 "
            "reads, so a trip id can't be used to learn whether a private trip exists.",
            HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed validation: an explicit `null`, "
            "a `name` empty or over 100 characters after trimming, a `visibility` other than "
            "'public'/'private', or a `publicDelayHours` outside 0-168.",
            HTTPStatus.TOO_MANY_REQUESTS: "The `writes` limit: 600 requests an hour per "
            "account, or per client address for a request with no session. Checked before the "
            "session, the trip and the membership, so nothing was changed. Retry after "
            "`Retry-After` seconds.",
        }
    ),
    description="""
**Context.** A leader's trip settings: its name, whether it is public, and how
long stops stay hidden from the public (decision-log Entry 29).

**How it works.** `limit_writes` runs first, then `require_trip_leader`: a valid
session (401, for every trip id alike), then the trip (404 if it doesn't exist,
or if it is private and the caller has no membership row on it -- the same 404
the v2 reads give), then an active **leader** membership (403). Only the fields
present in the body change; omitting a field is how "no change" is spelled, an
explicit `null` is a 422, and an empty body is a 200 with nothing changed.
There is no server-side publish confirmation. Last write wins.

**Related APIs.** `GET /api/v2/trips/{tripId}` reads the settings back;
`POST /api/v2/trips` creates the trip.
""",
)
async def patch_trip(
    context: Annotated[TripWriterContext, Depends(require_trip_leader)],
    session: SessionDep,
    body: TripPatch,
) -> TripOut:
    """Apply a partial update to the trip's settings."""
    trip = await trips_repo.update_settings(
        session, context.trip.id, body.model_dump(exclude_unset=True, mode="json")
    )
    return await trip_out(session, trip, context.viewer_role)


# --------------------------------------------------------------------------
# Trip-scoped reads: `require_trip_reader`, `public-read`, HEAD siblings
# --------------------------------------------------------------------------

ReaderDep = Annotated[TripReaderContext, Depends(require_trip_reader)]

TRIP_404 = (
    "No trip has this id, **or** the trip is private and the caller is not an active member. "
    "The two are byte-identical -- status, body and headers -- so a trip id can't be used to "
    "learn whether a private trip exists. Never a 401: a read needs no session."
)

GATE = """`{tripId}` is resolved by `require_trip_reader`: the trip must exist and be
public, or the caller must be an active member (rider or leader); anything else
is a 404 identical to a trip id that doesn't exist. The session is optional and
never produces a 401. Active members see everything with no delay. Anyone else
sees only stops with `arrivedAt <= now() - publicDelayHours`, on the server
clock at request time."""


def _read_route(path: str, handler, **kwargs) -> None:
    """Register ``handler`` for GET (documented) and HEAD (schema-excluded) on ``path``."""
    router.add_api_route(
        path, handler, methods=["GET"], dependencies=[Depends(limit_public_read)], **kwargs
    )
    router.add_api_route(
        path,
        handler,
        methods=["HEAD"],
        dependencies=[Depends(limit_public_read)],
        include_in_schema=False,
    )


async def get_trip(context: ReaderDep, session: SessionDep) -> TripOut:
    """The trip, as the legacy GET builds it, with ``viewer.role`` from the reader gate."""
    return await trip_out(session, context.trip, context.viewer_role)


_read_route(
    "/{tripId}",
    get_trip,
    summary="Get a trip",
    response_description="The trip, its bikes, and what the caller is to it.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description=f"""
**Context.** A trip's page, located by its id rather than a slug (decision-log
Entry 29). Anyone can open a public trip; a private one is visible to its
active members only.

**How it works.** {GATE} `viewer.role` is computed from the session on every
request: `anonymous`, `none`, `pending`, `rider` or `leader`. `access` is
deprecated and is `rider` exactly when the role is `rider` or `leader`.
`riderCount` and `lastPublicStopAt` are the same for every caller, members
included; `lastPublicStopAt` is the latest `arrivedAt` among stops visible to
the public. Bikes are not delayed. No slug, username or user id is in the
response.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/stops`, `/map`, `/bikes` and
`/stops/{{stopId}}/photos` for the rest of the trip; `GET /api/v2/trips` for the
public list.
""",
)


async def list_bikes(context: ReaderDep, session: SessionDep) -> list[BikeOut]:
    """Every bike on the trip. Not delayed, for anyone."""
    return await bikes_repo.list_by_trip(session, context.trip.id)


_read_route(
    "/{tripId}/bikes",
    list_bikes,
    summary="List a trip's bikes",
    response_description="Every bike on this trip.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description=f"""
**Context.** The bikes on a trip, with the rider name on each. The same list
`TripOut.bikes` embeds.

**How it works.** {GATE} Bikes are **not** delayed: every caller who may see the
trip sees every bike. A trip with no bikes is a `200` with `[]`. Order is for
response stability only; match bikes by `id`.

**Related APIs.** `GET /api/v2/trips/{{tripId}}` embeds the same list.
""",
)


async def list_stops(context: ReaderDep, session: SessionDep) -> list[StopOut]:
    """The trip's stops, delay-filtered for a non-member."""
    return await stops_repo.list_by_trip(
        session, context.trip.id, public_delay_hours=context.public_delay_hours
    )


_read_route(
    "/{tripId}/stops",
    list_stops,
    summary="List a trip's stops",
    response_description="The stops on this trip the caller may see.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description=f"""
**Context.** The journal itself: where the riders got to, when, and what they
wrote. The only v2 read that serves `notes` and `locationSource`.

**How it works.** {GATE} A stop hidden by the delay is simply absent, and a stop
whose `arrivedAt` is in the future is hidden from non-members even at a delay
of 0. A trip with no visible stops is a `200` with `[]`. Order is for response
stability only; match stops by `id`.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/map` for the same stops as
GeoJSON, `GET /api/v2/trips/{{tripId}}/stops/{{stopId}}/photos` for a stop's
photos.
""",
)


async def list_photos(
    context: ReaderDep,
    session: SessionDep,
    stop_id: Annotated[
        str,
        Path(
            alias="stopId",
            description="The stop's id. A stop on another trip, or one hidden from the caller "
            "by the public delay, is answered exactly as a stop that doesn't exist: 404.",
        ),
    ],
) -> list[PhotoOut]:
    """The stop's photos, if the stop is on this trip and visible to the caller."""
    if not await photos_repo.stop_belongs_to_trip(
        session, context.trip.id, stop_id, public_delay_hours=context.public_delay_hours
    ):
        raise ApiError.not_found(STOP_NOT_FOUND_MESSAGE)
    return await photos_repo.list_by_stop(session, stop_id)


_read_route(
    "/{tripId}/stops/{stopId}/photos",
    list_photos,
    summary="List a stop's photos",
    response_description="Every photo on this stop, each with a presigned URL.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: TRIP_404 + " Also: the stop does not exist on this trip, or "
            "is hidden from the caller by the public delay -- the same 404 for both.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description=f"""
**Context.** The photos attached to one stop, each with a presigned URL into
object storage, generated fresh per request and valid for an hour.

**How it works.** {GATE} A photo is visible exactly when its stop is: its own
`takenAt` plays no part. A stop hidden by the delay answers `404`, the same as a
stop that doesn't exist on this trip. A visible stop with no photos is a `200`
with `[]`.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/stops` for the stops.
""",
)


async def get_map(context: ReaderDep, session: SessionDep) -> MapFeatureCollection:
    """The trip's GeoJSON, built from the stops the caller may see."""
    return await stops_repo.map_features(
        session, context.trip.id, public_delay_hours=context.public_delay_hours
    )


_read_route(
    "/{tripId}/map",
    get_map,
    summary="GeoJSON map of a trip's stops and trail",
    response_description="A GeoJSON FeatureCollection: one Point per visible stop, and a "
    "LineString trail when 2 or more are visible.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: TRIP_404,
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description=f"""
**Context.** What the map renders: a pin per stop and a trail in arrival order.

**How it works.** {GATE} Pins **and** trail are built from the visible stops
only: the trail is drawn only when 2 or more stops are visible, so a non-member
never sees a line leading towards a hidden stop. Coordinates are
`[longitude, latitude]`. A trip with no visible stops is a `200` with
`features: []`.

**Related APIs.** `GET /api/v2/trips/{{tripId}}/stops` for each stop's notes and
`locationSource`, matched by `Feature.id`.
""",
)
