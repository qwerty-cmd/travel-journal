from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, Field


class LocationSource(StrEnum):
    GPS = "gps"  # navigator.geolocation fix, captured automatically on "Add stop"
    MANUAL = "manual"  # permission denied / no fix — rider tapped a point on the map instead


class StopCreate(BaseModel):
    """POST /trips/{slug}/stops body. Rider-slug only."""

    id: str = Field(
        description="Client-generated UUID4, assigned when the stop is captured "
        "(possibly offline). Replaying the same id returns the existing stop (200) "
        "instead of creating a duplicate — see docs/api-contract.md 'Idempotency'."
    )
    name: str = Field(description="What the rider called this stop, e.g. 'Daly Waters Pub'.")
    lat: float = Field(
        description="Latitude in decimal degrees, WGS 84. Note that GeoJSON positions in "
        "the map endpoint are ordered [lng, lat] — the reverse of this pair."
    )
    lng: float = Field(
        description="Longitude in decimal degrees, WGS 84. Note that GeoJSON positions in "
        "the map endpoint are ordered [lng, lat] — the reverse of this pair."
    )
    locationSource: LocationSource = Field(
        description="How lat/lng were obtained — an automatic GPS fix, or a manual "
        "map tap (spec Section 6, 'Add stop' fallback when GPS is denied/unavailable)."
    )
    # `AwareDatetime` and a bare `datetime` emit the *same* JSON Schema —
    # {"type": "string", "format": "date-time"}. So diffing the OpenAPI document makes the
    # aware type look like it buys nothing. True, and beside the point: the difference is at
    # parse time, not in the document. `AwareDatetime` rejects a naive value; a bare
    # `datetime` accepts it and hands it to a `timestamptz` column, where the session
    # `TimeZone` decides what hour it meant. A stop filed at the wrong hour is the failure,
    # and it is invisible in the schema. Do not relax this to `datetime`.
    # See docs/api-contract.md, "`StopCreate.arrivedAt` must be timezone-aware".
    arrivedAt: AwareDatetime = Field(
        description="When the rider arrived, as a timezone-aware ISO 8601 instant. Captured "
        "on the device, which may have been offline, so it is the arrival time and not the "
        "time the server received the stop. The UTC offset is **required** — a naive value "
        "(no offset) is rejected with 422 / VALIDATION_ERROR rather than assumed to be UTC "
        "or server-local, because a rider crossing timezones has no offset worth guessing. "
        "The generated client cannot catch this; only the server rejects it."
    )
    notes: str | None = Field(
        default=None,
        description="Whatever the rider wants to write about this stop. Optional — omit the "
        "field entirely or send **null**; both mean 'no notes' and are stored as SQL NULL.",
    )


class StopOut(BaseModel):
    """One stop as returned. The elements of `GET /trips/{slug}/stops`."""

    id: str = Field(
        description="The stop's id — the same client-generated UUID4 that was sent on "
        "create, so the device's local copy and the server's record share one identifier. "
        "Match stops by this, never by their position in the list. It is also what "
        "`GET /trips/{slug}/map` puts on each Point feature's top-level `id`, so a clicked "
        "pin resolves back to the full stop here."
    )
    name: str = Field(description="What the rider called this stop, e.g. 'Daly Waters Pub'.")
    lat: float = Field(
        description="Latitude in decimal degrees, WGS 84. Note that GeoJSON positions in "
        "the map endpoint are ordered [lng, lat] — the reverse of this pair."
    )
    lng: float = Field(
        description="Longitude in decimal degrees, WGS 84. Note that GeoJSON positions in "
        "the map endpoint are ordered [lng, lat] — the reverse of this pair."
    )
    locationSource: LocationSource = Field(
        description="How lat/lng were obtained — 'gps' for an automatic fix, 'manual' where "
        "the rider tapped a point on the map because geolocation was denied or unavailable. "
        "Always present; there is no 'unknown' third value. It lets a viewer tell an exact "
        "fix from an approximate tap. Stored as `stops.location_source`."
    )
    arrivedAt: datetime = Field(
        description="When the rider arrived, as a timezone-aware ISO 8601 instant. Captured "
        "on the device, which may have been offline, so it is the arrival time and not the "
        "time the server received the stop. Stored as `stops.arrived_at`."
    )
    notes: str | None = Field(
        description="Whatever the rider wrote about this stop, or **null** when they wrote "
        "nothing — unlike `BikeOut.specs`, this column is nullable and null is the empty "
        "case. This endpoint is the only one that serves it; the map endpoint omits it."
    )
