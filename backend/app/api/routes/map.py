"""
Map route -- ``GET /api/trips/{slug}/map``, the GeoJSON the Leaflet map renders.

The handler is a one-liner: the repository builds the entire
``MapFeatureCollection`` from the stops table, and the slug dependency handles
access control.  This file adds no column names and imports no SQLAlchemy.
"""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.responses import PATH_PARAMETERS_422, PUBLIC_READ_429, error_responses
from app.core.ratelimit import limit_public_read
from app.core.security import TripContext, require_trip_access
from app.data.db import SessionDep
from app.data.repositories.stops import map_features
from app.models.map import MapFeatureCollection

router = APIRouter(prefix="/trips/{slug}/map", tags=["map"])


@router.get(
    "",
    dependencies=[Depends(limit_public_read)],
    summary="GeoJSON map of a trip's stops and trail",
    response_description="A GeoJSON FeatureCollection with one Point per stop and an "
    "optional LineString trail connecting them chronologically.",
    responses=error_responses(
        {
            HTTPStatus.NOT_FOUND: "No trip has this slug. Deliberately the same answer for a "
            "mistyped link, a revoked one and a guess.",
            HTTPStatus.UNPROCESSABLE_ENTITY: PATH_PARAMETERS_422,
            HTTPStatus.TOO_MANY_REQUESTS: PUBLIC_READ_429,
        }
    ),
    description="""
**Context.** This is what the Leaflet map renders: one pin per stop, plus a
polyline trail connecting them in the order the rider arrived.  Either slug
accepted -- it is a read.  Task `t-map-geojson-endpoint`.

**How it works.** `{slug}` is resolved by `require_trip_access` (either slug,
404 on unknown).  The repository fetches only the columns the map needs --
`id`, `name`, `lat`, `lng`, `arrived_at` -- ordered chronologically, builds
the GeoJSON features (coordinates in `[longitude, latitude]` order per the
GeoJSON spec), and appends the trail LineString when 2+ stops exist.  A trip
with no stops is a `200` with `features: []`.

**Related APIs.** `GET /api/trips/{slug}/stops` serves the full stop data
(notes, locationSource) the frontend resolves a clicked pin against by id.
`GET /api/trips/{slug}` for the trip header.
""",
)
async def get_map(
    context: Annotated[TripContext, Depends(require_trip_access)],
    session: SessionDep,
) -> MapFeatureCollection:
    """The GeoJSON FeatureCollection for this trip's map."""
    return await map_features(session, context.trip.id)


router.add_api_route(
    "",
    get_map,
    methods=["HEAD"],
    dependencies=[Depends(limit_public_read)],
    include_in_schema=False,
)
