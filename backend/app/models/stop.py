from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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
    name: str
    lat: float
    lng: float
    locationSource: LocationSource = Field(
        description="How lat/lng were obtained — an automatic GPS fix, or a manual "
        "map tap (spec Section 6, 'Add stop' fallback when GPS is denied/unavailable)."
    )
    arrivedAt: datetime
    notes: str | None = None


class StopOut(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    locationSource: LocationSource
    arrivedAt: datetime
    notes: str | None
