from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.bike import BikeOut


class Access(StrEnum):
    RIDER = "rider"
    VIEWER = "viewer"


class TripOut(BaseModel):
    """GET /trips/{slug} response. Works with either slug."""

    id: str
    name: str
    startDate: date
    bikes: list[BikeOut]
    access: Access = Field(
        description="Which kind of slug was used to make this request — 'rider' or "
        "'viewer'. The frontend uses this, not a stored user role, to decide whether "
        "to show write UI (Add stop, upload photo, edit bikes)."
    )
