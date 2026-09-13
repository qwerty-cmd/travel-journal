from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.bike import BikeOut


class Access(StrEnum):
    RIDER = "rider"
    VIEWER = "viewer"


class TripOut(BaseModel):
    """GET /trips/{slug} response. Works with either slug."""

    id: str = Field(
        description="The trip's id. Stable, and safe to show or log — it is not a "
        "credential and cannot be used to reach the trip. Neither slug appears "
        "anywhere in this response."
    )
    name: str = Field(description="The trip's display name, as the rider titled it.")
    startDate: date = Field(
        description="The day the trip starts, as an ISO 8601 date (YYYY-MM-DD). A date "
        "with no time and no timezone: it is the calendar day the journal opens on, not "
        "an instant, so it reads the same wherever the viewer is."
    )
    bikes: list[BikeOut] = Field(
        description="The bikes on **this** trip, embedded so the app's first load is one "
        "request rather than two. Empty list when no bikes have been added yet — that is a "
        "normal trip, not an error. Order is not part of the contract; match bikes by `id`."
    )
    access: Access = Field(
        description="Which kind of slug was used to make this request — 'rider' or "
        "'viewer'. The frontend uses this, not a stored user role, to decide whether "
        "to show write UI (Add stop, upload photo, edit bikes)."
    )
