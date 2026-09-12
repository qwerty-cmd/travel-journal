"""
Response contract for `GET /trips/{slug}/map` — the GeoJSON the Leaflet map
renders (spec Sections 4 and 5). Works with either slug.

The response is a single GeoJSON FeatureCollection holding two kinds of
feature: one Point feature per stop (the pins), plus at most one LineString
feature (the trail — the polyline auto-connecting stops in chronological
order, spec Section 4). The frontend tells them apart by `geometry.type`
alone; there is deliberately no extra `kind`/`featureType` tag.

What this module does NOT cover, so the boundary is explicit:

- **Ordering.** Sorting stops chronologically by `arrivedAt` before building
  the trail's coordinate list is the route handler's job. This file encodes
  the *shape* and the ">= 2 positions" constraint only — nothing here can or
  does enforce that the positions arrive in the right order.
- **When the trail exists.** The trail feature is included only when the trip
  has 2+ stops (a single stop can't form a line). That's also a handler
  decision; the model just allows a features list with or without it.
- **Zero stops.** A trip with no stops is a valid `200` with
  `features: []` — an empty map, not an error.
- **Unknown slug.** A nonexistent trip slug is a `404` with the standard
  `ErrorEnvelope` (app.models.common) — an endpoint concern, out of scope
  for these models.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PointGeometry(BaseModel):
    """GeoJSON Point geometry — the location of a single stop pin."""

    type: Literal["Point"] = Field(
        default="Point",
        description="GeoJSON geometry type discriminator. Always 'Point' for a stop pin.",
    )
    # NOTE: GeoJSON positions are [longitude, latitude] — the REVERSE of the
    # Stop.lng / Stop.lat field order used everywhere else in this API (and of
    # Leaflet's own LatLng). Swapping them does not raise: both are plain
    # floats, so a wrong order silently plots the pin somewhere else on Earth.
    coordinates: list[float] = Field(
        min_length=2,
        max_length=2,
        description="Position as [longitude, latitude] — note the order is reversed "
        "from the Stop model's lat/lng fields, per the GeoJSON spec.",
    )


class LineStringGeometry(BaseModel):
    """GeoJSON LineString geometry — the trail connecting stops in chronological order."""

    type: Literal["LineString"] = Field(
        default="LineString",
        description="GeoJSON geometry type discriminator. Always 'LineString' for the trail.",
    )
    # Same [longitude, latitude] reversal as PointGeometry above. min_length=2
    # is the GeoJSON spec's own rule (a line needs at least two positions), so
    # it belongs here rather than in the handler.
    coordinates: list[list[float]] = Field(
        min_length=2,
        description="Ordered list of [longitude, latitude] positions, one per stop, "
        "earliest arrivedAt first. At least 2 positions — a LineString with fewer is "
        "invalid GeoJSON, which is why a trip with 0 or 1 stops omits this feature "
        "entirely instead of emitting an empty trail.",
    )


class StopFeatureProperties(BaseModel):
    """
    Properties carried by a stop's Point feature — exactly the two values the
    map needs to label a pin.

    Deliberately does NOT include `notes` or `locationSource`. On pin click the
    frontend looks the full stop up by id in the already-fetched `/stops` list
    (spec Section 5), so duplicating those fields here would mean the same data
    served by two endpoints, with two chances to drift.
    """

    name: str = Field(description="Stop name, shown as the pin's label/popup title.")
    arrivedAt: datetime = Field(
        description="When the rider arrived at this stop; also the trail's sort key."
    )


class TrailFeatureProperties(BaseModel):
    """
    Properties of the trail feature — intentionally empty.

    GeoJSON requires a `properties` member on every Feature, but the trail
    carries no metadata of its own: it's derived entirely from the stop
    features in the same collection.
    """


class StopFeature(BaseModel):
    """GeoJSON Feature for one stop — a map pin."""

    type: Literal["Feature"] = Field(
        default="Feature", description="GeoJSON object type. Always 'Feature'."
    )
    id: str = Field(
        description="The stop's id (Stop.id), carried in GeoJSON's own top-level "
        "Feature id member rather than duplicated into properties. This is what the "
        "frontend matches against the /stops list when a pin is clicked."
    )
    geometry: PointGeometry = Field(
        description="Where the pin sits — see PointGeometry for the [lng, lat] order."
    )
    properties: StopFeatureProperties = Field(
        description="Pin label data: name and arrival time only."
    )


class TrailFeature(BaseModel):
    """
    GeoJSON Feature for the trail — the polyline auto-connecting the trip's
    stops in chronological order. Present only when the trip has 2+ stops.

    Has no top-level `id`: it isn't a stored entity, it's derived from the
    stops each time the endpoint is called.
    """

    type: Literal["Feature"] = Field(
        default="Feature", description="GeoJSON object type. Always 'Feature'."
    )
    geometry: LineStringGeometry = Field(
        description="The route line through every stop, earliest arrivedAt first."
    )
    properties: TrailFeatureProperties = Field(
        default_factory=TrailFeatureProperties,
        description="Always an empty object — the trail carries no metadata of its own.",
    )


class MapFeatureCollection(BaseModel):
    """
    `GET /trips/{slug}/map` response — one GeoJSON FeatureCollection, handed
    straight to Leaflet. Works with either slug (spec Sections 4 and 5).
    """

    type: Literal["FeatureCollection"] = Field(
        default="FeatureCollection",
        description="GeoJSON object type. Always 'FeatureCollection'.",
    )
    # Plain union, no discriminator field: the two member shapes differ enough
    # (Point vs LineString geometry) that GeoJSON's own geometry.type is all
    # the frontend needs to branch on.
    features: list[StopFeature | TrailFeature] = Field(
        description="One Point feature per stop, plus the single LineString trail "
        "feature when the trip has 2+ stops. Empty list for a trip with no stops — "
        "that's a valid 200, not an error."
    )
