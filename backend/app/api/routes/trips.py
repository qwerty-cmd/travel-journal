"""
Trip routes — ``GET /api/trips/{slug}``, the first request the app ever makes.

The handler below is deliberately thin: the read gate has already resolved the
trip and derived ``access`` from the caller's membership, and the repository layer owns every
column name. What is left here is the mapping from those two values onto the
contract's ``TripOut``, written out field by field (see the handler's own note
on why it is spelled out rather than spread).
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.core.ratelimit import limit_public_read
from app.core.security import TripContext, require_trip_access
from app.data.db import SessionDep
from app.data.repositories.bikes import list_by_trip
from app.data.repositories.trips import TripRecord, get_stats
from app.models.trip import Access, TripOut, ViewerOut, ViewerRole

router = APIRouter(prefix="/trips", tags=["trips"])


async def trip_out(session: SessionDep, trip: TripRecord, role: ViewerRole) -> TripOut:
    """
    ``TripOut`` for ``trip`` as seen by a caller with ``role``.

    Shared with ``GET /api/v2/trips/{tripId}``.

    Built by explicit keyword, never by spreading the record (see ``get_trip``).
    ``access`` is derived from ``role`` exactly as the contract states it:
    ``rider`` when the role is ``rider`` or ``leader``, ``viewer`` otherwise.
    ``riderCount`` and ``lastPublicStopAt`` are the same for every caller.
    """
    stats = await get_stats(session, trip.id)
    return TripOut(
        id=trip.id,
        name=trip.name,
        startDate=trip.start_date,
        bikes=await list_by_trip(session, trip.id),
        access=Access.RIDER if role in (ViewerRole.RIDER, ViewerRole.LEADER) else Access.VIEWER,
        visibility=trip.visibility,
        publicDelayHours=trip.public_delay_hours,
        riderCount=stats.rider_count,
        lastPublicStopAt=stats.last_public_stop_at,
        viewer=ViewerOut(role=role),
    )


@router.get(
    "/{slug}",
    dependencies=[Depends(limit_public_read)],
    summary="Get a trip's metadata and its bikes",
    response_description="The trip, the bikes on it, and whether the caller is an active member.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: "No trip has this slug. Deliberately the same answer for a "
            "mistyped link, a revoked one and a guess — see `docs/api-contract.md`, "
            "'Access control: 403 and 404 are different answers'.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** A trip from before accounts was shared through two links, a rider
link and a viewer link. Since decision-log Entry 29 either link only *locates*
the trip; what the caller may do is decided by their membership. This is the
endpoint both links open first. It answers three questions in one round trip —
which trip is this, which bikes are on it, and should the UI offer writes —
because the app is used on the Stuart Hwy where a second request is a second
chance to be offline. Tasks `t-trip-metadata-endpoint`, `t-am-write-gate-legacy`.

**How it works.** `{slug}` is resolved by the shared `require_trip_access`
dependency, which accepts **either** slug and raises a 404 if neither matches.
The session is optional here and never produces a 401. The dependency derives
`access`: `rider` iff the session user has an active membership on this trip
(read fresh on every request), `viewer` for everyone else — anonymous, a
signed-in non-member, a pending requester or a revoked member. Which slug was
followed plays no part. `viewer.role` is derived alongside it (`anonymous`,
`none`, `pending`, `rider` or `leader`), and `access` is `rider` exactly when
that role is `rider` or `leader`. `visibility`, `publicDelayHours`, `riderCount`
and `lastPublicStopAt` are filled as on `GET /api/v2/trips/{tripId}`; this read
itself stays full and undelayed for either slug. The trip's bikes are fetched by `data/repositories/bikes.list_by_trip`,
filtered to this trip; a trip with no bikes returns `"bikes": []`, which is a
normal trip and not a 404. Neither slug is in the response: `TripOut` has no
slug field, and the slug is the credential.

`access` is a **UI hint**, deprecated, and not the enforcement point. It tells
the frontend whether to render Add stop / upload photo / edit bikes. The write
endpoints check the session and membership themselves regardless of what the UI
chose to show.

**Related APIs.** `GET /api/trips/{slug}/stops` for the stops this header sits
above, `GET /api/trips/{slug}/map` for the same trip as GeoJSON, and
`POST /api/trips/{slug}/bikes` / `PATCH /api/trips/{slug}/bikes/{id}` for the
member-only writes behind the `bikes` list returned here.
""",
)
async def get_trip(
    context: Annotated[TripContext, Depends(require_trip_access)],
    session: SessionDep,
) -> TripOut:
    """
    The trip behind this slug, with its bikes and the caller's access level.

    Built by explicit keyword in ``trip_out``, not by spreading the record. Three reasons, and
    they are the reason the next seven handlers should look like this one:

    - The wire names are camelCase and the columns are snake_case
      (``tables.py``). ``startDate=trip.start_date`` is the mapping,
      stated once, where both halves are visible together.
    - ``TripRecord`` carries **both slugs**. A spread — ``TripOut(**asdict(...))``
      or ``model_validate(record)`` — is a construct that reaches for every
      attribute of a record whose extra attributes are the app's only
      credentials. Naming four fields cannot pick up a fifth by accident.
    - ``access`` is not on the record at all. It is a property of the *request*
      (whether its session user is an active member), so it can only come from
      the dependency's ``TripContext`` (``viewer_role``), never from the row.
    """
    return await trip_out(session, context.trip, context.viewer_role)


# HEAD is GET without a body (RFC 9110 §9.3.2), so the same handler serves it:
# `require_trip_access` still runs, which is the point — an unknown slug is still
# a 404 and a viewer slug is still a success, and the ASGI server drops the body.
#
# A **second registration** rather than `methods=["GET", "HEAD"]` on the route
# above: `fastapi.openapi.utils.get_openapi_path` loops `for method in
# route.methods` with no HEAD exclusion while `operation_id` is per-*route*, so
# one route carrying both verbs emits a duplicate `head:` operation into the
# OpenAPI document — plus a "Duplicate Operation ID" warning — and Kubb would
# generate a second, identical `useGetTrip` hook from it. `include_in_schema=False`
# keeps the document to the one operation `docs/api-contract.md` describes.
# (Measured on FastAPI 0.141.1.)
router.add_api_route(
    "/{slug}",
    get_trip,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)
