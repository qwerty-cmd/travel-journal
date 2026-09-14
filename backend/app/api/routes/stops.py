"""
Stop routes — ``GET /api/trips/{slug}/stops``, the journal's actual content.

The handler below is deliberately thin, the same shape ``trips.py`` set: the
slug dependency has already resolved the trip, and the repository layer owns
every column name and builds every statement. Nothing here names a column or
imports SQLAlchemy.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import TripContext, require_trip_access
from app.data.db import SessionDep
from app.data.repositories.stops import list_by_trip
from app.models.common import ErrorEnvelope
from app.models.stop import StopOut

# GET/POST /trips/{slug}/stops — list/create stops (POST is rider-slug only,
# 403 on viewer slug). POST lands with its own task.
router = APIRouter(prefix="/trips/{slug}/stops", tags=["stops"])


@router.get(
    "",
    summary="List a trip's stops",
    response_description="Every stop on this trip, with its notes and how it was located.",
    responses={
        HTTPStatus.NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No trip has this slug. Deliberately the same answer for a "
            "mistyped link, a revoked one and a guess — see `docs/api-contract.md`, "
            "'Access control: 403 and 404 are different answers'. A trip that exists but "
            "has no stops yet is **not** this case: that is a `200` with `[]`.",
        }
    },
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
`POST /api/trips/{slug}/stops` to add a stop (rider slug only),
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
router.add_api_route("", list_stops, methods=["HEAD"], include_in_schema=False)
