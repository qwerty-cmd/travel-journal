"""
Stop routes — ``GET``/``POST /api/trips/{slug}/stops``, the journal's actual content.

The handlers below are deliberately thin, the same shape ``trips.py`` set: the
slug dependency has already resolved the trip, and the repository layer owns
every column name and builds every statement. Nothing here names a column or
imports SQLAlchemy.

The two handlers declare **different dependencies**, and that difference is the
whole of the access model on this path: ``GET`` takes ``require_trip_access``
(either slug — it is a read), ``POST`` takes ``require_trip_writer`` (either slug
locates the trip; a signed-in active member writes, 401 without a session, 403
for anyone else). They are otherwise identically shaped, so the dependency is the
only thing standing between a stranger and a write.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.responses import (
    PATH_PARAMETERS_422,
    PUBLIC_READ_429,
    WRITES_429,
    error_responses,
)
from app.core.errors import ApiError
from app.core.ratelimit import limit_public_read, limit_writes
from app.core.security import (
    TripContext,
    TripWriterContext,
    require_trip_access,
    require_trip_writer,
)
from app.data.db import SessionDep
from app.data.repositories.stops import StopIdOnAnotherTrip, create, list_by_trip
from app.models.stop import StopCreate, StopOut

# GET/POST /trips/{slug}/stops — list/create stops (POST needs an active member's
# session; either slug only locates the trip).
router = APIRouter(prefix="/trips/{slug}/stops", tags=["stops"])

# Shown when the id in the body is already a stop on a different trip. Says that
# the id is taken and that nothing was stored, and **nothing else** — not the
# other trip's slug, id or name, not the other stop's fields. A 409 already tells
# the caller the id exists somewhere; anything more is disclosure of a record the
# caller's link does not authorise (decision-log Entry 14). The raise site holds
# that by never reading the conflicting row.
ID_ALREADY_USED_MESSAGE = (
    "This stop couldn't be saved: its id is already in use on another trip. Nothing was "
    "changed here. Sending it again unchanged will keep failing — it needs a new id."
)

# A NUL byte in the body's `id`: no stored id can contain one.
ID_HAS_NUL_MESSAGE = (
    "This stop couldn't be saved: its id contains a NUL character, which no id can hold. "
    "Nothing was stored. Sending it again unchanged will keep failing — it needs a new id."
)


@router.get(
    "",
    dependencies=[Depends(limit_public_read)],
    summary="List a trip's stops",
    response_description="Every stop on this trip, with its notes and how it was located.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: "No trip has this slug. Deliberately the same answer for a "
            "mistyped link, a revoked one and a guess — see `docs/api-contract.md`, "
            "'Access control: 403 and 404 are different answers'. A trip that exists but "
            "has no stops yet is **not** this case: that is a `200` with `[]`.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** The stops are the journal — where the rider got to, when, and what
they wrote about it. `GET /api/trips/{slug}` is the header above this list;
this is the body. It is also the **only** endpoint that serves `notes` and
`locationSource`: `GET /api/trips/{slug}/map` deliberately omits both so the
same data is not served from two places and given two chances to drift, and the
frontend resolves a clicked pin back to its full stop from this list by `id`.
Task `t-stops-list-endpoint`.

**How it works.** `{slug}` is resolved by the shared `require_trip_access`
dependency, which accepts **either** slug and raises a 404 if neither matches; a
viewer slug is an ordinary success here, not a 403, because this is a read. The
stops are fetched by `data/repositories/stops.list_by_trip`, filtered on the
`trip_id` the dependency resolved rather than on anything the caller typed, so
no other trip's stops can appear. **A trip with no stops is a `200` with `[]`** —
an empty collection, not a missing one, and never a 404. Neither slug is in the
response: `StopOut` has no slug field, and the slug is the credential.

The list comes back ordered by `arrivedAt` then `id`, but that is for response
stability only and is **not** part of the contract — match stops by `id`, never
by position. The map endpoint's chronological trail is established by the map
handler itself and does not read its ordering guarantee from here.

**Related APIs.** `GET /api/trips/{slug}` for the trip header and its bikes,
`POST /api/trips/{slug}/stops` to add a stop (active members only),
`GET /api/trips/{slug}/stops/{id}/photos` for one stop's photos, and
`GET /api/trips/{slug}/map` for the same stops as GeoJSON.
""",
)
async def list_stops(
    context: Annotated[TripContext, Depends(require_trip_access)],
    session: SessionDep,
) -> list[StopOut]:
    """
    Every stop on the trip behind this slug.

    The whole handler is the hand-off: the guard produced the trip, the
    repository produces the contract models. ``context.trip.id`` is the only
    value passed down, and it came from the resolved row — never from a path or
    query parameter — which is what confines the result to this trip.
    """
    return await list_by_trip(session, context.trip.id)


# HEAD is GET without a body (RFC 9110 §9.3.2), so the same handler serves it:
# `require_trip_access` still runs, which is the point — an unknown slug is still
# a 404 and a viewer slug is still a success, and the ASGI server drops the body.
#
# A **second registration** rather than `methods=["GET", "HEAD"]` on the route
# above: `fastapi.openapi.utils.get_openapi_path` loops `for method in
# route.methods` with no HEAD exclusion while `operation_id` is per-*route*, so
# one route carrying both verbs emits a duplicate `head:` operation into the
# OpenAPI document — plus a "Duplicate Operation ID" warning — and Kubb would
# generate a second, identical `useListStops` hook from it. `include_in_schema=False`
# keeps the document to the one operation `docs/api-contract.md` describes.
# (Measured on FastAPI 0.141.1; decision-log Entry 11.)
router.add_api_route(
    "",
    list_stops,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)


# No HEAD sibling here, and that is not an omission: HEAD is GET without a body
# (RFC 9110 §9.3.2), so the convention two blocks up is a property of *read*
# routes. A HEAD that ran a create would store a stop and throw the response
# away.
@router.post(
    "",
    dependencies=[Depends(limit_writes)],
    status_code=HTTPStatus.CREATED,
    summary="Add a stop to a trip",
    response_description="The stop as stored — the one just created (201), or the one "
    "already stored under this id (200).",
    responses={
        HTTPStatus.OK: {
            "model": StopOut,
            "description": "**A replay, not a second stop.** This id is already a stop on "
            "this trip, so nothing was created and the **stored** record is returned "
            "unchanged — even where this request's body differs from it. A success, not an "
            "error: the offline queue can resend a create it never saw confirmed, any number "
            "of times, and reconcile against whichever attempt landed.",
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
                HTTPStatus.NOT_FOUND: "No trip has this slug. Deliberately the same answer for a "
                "mistyped link, a revoked one and a guess, and checked before the session — see "
                "`docs/api-contract.md`, 'Access control: 401, 403 and 404 are three different "
                "answers'.",
                HTTPStatus.CONFLICT: "The `id` in the body already belongs to a stop on a "
                "**different** trip, so this is not a replay. Nothing was created, and nothing "
                "about the conflicting record is disclosed — not in the body, not in the "
                "message. Never-retry for the offline queue: the same id will conflict on every "
                "future attempt, so the stop needs a new one.",
                HTTPStatus.UNPROCESSABLE_ENTITY: "The body failed schema validation — a missing "
                "or mistyped field, a `locationSource` outside `gps`/`manual`, or an "
                "`arrivedAt` with **no UTC offset**. That last one is server-enforced only; the "
                "generated client types it as a plain string and cannot catch it. Also an `id` "
                "containing a NUL character, which no stored id can hold.",
                HTTPStatus.TOO_MANY_REQUESTS: WRITES_429,
            }
        ),
    },
    description="""
**Context.** This is how a stop gets into the journal, and the one write the app
makes most: the rider taps "Add stop" on the Stuart Hwy, often with no signal, so
the stop is captured on the device with an id the *client* generates and queued
until there is a connection. Only a signed-in, active member of the trip can add
one; either slug just locates the trip (decision-log Entry 29, contract default
21). Tasks `t-stops-create-endpoint`, `t-am-write-gate-legacy`.

**How it works.** `require_trip_writer` checks, in order: the slug (404 if no
trip has it), the session (401), and an active membership on the trip, read fresh
on every request (403 "You're not a rider on this trip." or, for a revoked
member, "You're no longer a rider on this trip."). The stop is stored against the
`trip_id` that dependency resolved, never against anything in the path or the
body, with `created_by` set to the signed-in account. `StopCreate.id` then decides
one of **three** branches
(`docs/api-contract.md`, "Idempotency"; decision-log Entry 14):

- an **unseen** id creates the stop — `201`, body is the new stop;
- an id **already on this trip** is a replay — `200`, body is the **stored**
  record, and a body that differs from the stored one changes nothing;
- an id that exists **on a different trip** is not a replay — `409` /
  `CONFLICT`, nothing is created, and nothing about the other record is
  disclosed.

The replay is matched on `(trip_id, id)` rather than on the id alone, so no
other trip's stop can be returned through this trip's slug, and the third branch
is decided by a check *before* the insert rather than by letting the insert fail
— a driver error would render `500` / `INTERNAL_ERROR`, which the offline queue
retries forever.

`arrivedAt` is the time the rider arrived, not the time this request was
received, and its **UTC offset is required**: a naive value is a `422`, because a
rider crossing timezones has no offset worth guessing (decision-log Entry 15).

**Related APIs.** `GET /api/trips/{slug}/stops` lists what this endpoint writes,
`GET /api/trips/{slug}` is the trip header above it (its `access` field is the
UI hint for whether to offer this write at all: `rider` iff the caller is an
active member),
`POST /api/trips/{slug}/stops/{id}/photos` attaches photos to a stop created
here, and `GET /api/trips/{slug}/map` renders these stops as GeoJSON.
""",
)
async def create_stop(
    context: Annotated[TripWriterContext, Depends(require_trip_writer)],
    session: SessionDep,
    stop: StopCreate,
    response: Response,
) -> StopOut:
    """
    Create the stop, or hand back the one this id already named on this trip.

    Two statuses from one route, which FastAPI cannot declare twice: `201` is the
    route's `status_code=`, and the replay's `200` is set on `response` here, at
    runtime. It is also declared in `responses=` above — without that the
    generated client would not know `200` is a legal success on this operation.

    The repository decides *which* branch; this handler only maps the branch onto
    a status. `StopIdOnAnotherTrip` carries no data, and the message below takes
    nothing from the conflicting record.
    """
    return await store_stop(session, context, stop, response)


async def store_stop(
    session: SessionDep, context: TripWriterContext, stop: StopCreate, response: Response
) -> StopOut:
    """
    The create after the gate, shared by the legacy and v2 routes.

    The three-way branch lives here once, so the two surfaces cannot answer the
    same id differently. A NUL byte in the client id is a ``422`` before any
    query: Postgres ``text`` cannot hold one, so the lookup would fail in the
    driver as a ``500``, which the offline queue retries forever.
    """
    if "\x00" in stop.id:
        raise ApiError.validation(ID_HAS_NUL_MESSAGE)
    try:
        created_stop, created = await create(
            session, context.trip.id, stop, created_by=context.user.user_id
        )
    except StopIdOnAnotherTrip:
        # `from None`: the chained context would be an internal exception in the
        # log for an ordinary, expected client condition.
        raise ApiError.conflict(ID_ALREADY_USED_MESSAGE) from None

    if not created:
        response.status_code = HTTPStatus.OK

    return created_stop
